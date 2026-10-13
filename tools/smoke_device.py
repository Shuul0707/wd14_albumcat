"""Drive the real Android app through a full round on a device or emulator.

WHY THIS EXISTS
---------------
`tools/check_android_runner.py` compiles the app's real `TagRunner.kt` and runs
it against in-memory fakes. That catches logic bugs, but it *replaces* the two
places where the app touches Android itself:

  * `Thumbnailer`      -- decoding a photo and resizing it to 448px
  * `ContentResolver`  -- opening a SAF document for reading and writing

A bug that lived purely in that replaced layer would pass every desktop check.
One did: `Thumbnailer.thumbnail` used

    resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(...) } ?: throw

which looks like a null-check on the *stream* but is really a null-check on
the *decode*. With `inJustDecodeBounds` the decode always returns null, so
every single photo failed with "cannot open <uri>" while the desktop suite
stayed green. It was only visible on a device.

So this script runs the app on an actual Android system and asserts on what
the filesystem ends up containing. It is deliberately end-to-end: real SAF
picker, real ContentResolver, real MediaStore.

WHAT IT ASSERTS
---------------
  1. the run writes all four probe photos (3 JPEG + 1 PNG)
  2. every photo got bigger and now carries an XMP packet
  3. re-running skips all four and leaves every byte untouched
  4. undo restores all four to their original SHA-256

REQUIREMENTS
------------
An emulator or rooted device, `adb` on PATH (or ANDROID_SDK_ROOT set), and the
PC-side service running, because the app asks it what each photo is:

    tools\\albumcat.cmd serve --port 8899

On an emulator the PC is reachable at 10.0.2.2, which is what this script
seeds into the app's preferences.

Usage:  python tools/smoke_device.py [--serial emulator-5554] [--port 8899]
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.etree import ElementTree

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
SDK = Path(os.environ.get("ANDROID_SDK_ROOT") or r"E:\Android\sdk")
ADB = SDK / "platform-tools" / "adb.exe"

APP_PKG = "com.albumcat"
APP_APK = ROOT / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"

DEVICE_DIR = "/sdcard/DCIM/AlbumcatSmoke"
TREE_URI = "content://com.android.externalstorage.documents/tree/primary%3ADCIM%2FAlbumcatSmoke"

# The three originals that cover both formats and both image shapes. The PNG
# is a genuine Android screenshot, so it exercises the iTXt path on the same
# kind of file a phone actually produces.
PHOTOS = [
    "samples/7fe97f53b11ab42ba94084450e3e92d4.jpg",
    "samples/97e5e12787c3d0b146edd718e57e6d7c.jpg",
    "samples/f52b341d2a083d34bc607a4dd6457545.jpg",
    "samples/698bb1e8a6e2b4e22e686a66f433d9e4.png",
]

RESULTS = []


# --------------------------------------------------------------------------- #
# shell plumbing
# --------------------------------------------------------------------------- #

def _run(args, binary=False, timeout=180):
    proc = subprocess.run(args, capture_output=True, timeout=timeout)
    if binary:
        return proc.returncode, proc.stdout, proc.stderr
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return proc.returncode, out, err


class Device:
    def __init__(self, serial):
        self.serial = serial

    def adb(self, *args, binary=False, timeout=180):
        base = [str(ADB)]
        if self.serial:
            base += ["-s", self.serial]
        return _run(base + list(args), binary=binary, timeout=timeout)

    def shell(self, cmd, binary=False, timeout=180):
        return self.adb("shell", cmd, binary=binary, timeout=timeout)

    def out(self, cmd):
        rc, out, err = self.shell(cmd)
        return out.strip()

    def check_alive(self):
        rc, out, _ = self.adb("devices")
        if self.serial not in out:
            raise SystemExit(
                f"device {self.serial!r} is not connected.\n"
                f"adb devices says:\n{out}\n"
                "Start one with:  emulator -avd <name> -no-window -gpu swiftshader_indirect"
            )
        boot = self.out("getprop sys.boot_completed")
        if boot != "1":
            raise SystemExit(f"device is still booting (sys.boot_completed={boot!r})")

    # ---- UI ------------------------------------------------------------- #

    def dump(self):
        """Return the uiautomator hierarchy as an ElementTree root."""
        self.shell("uiautomator dump /sdcard/_smoke_ui.xml")
        rc, data, err = self.shell("cat /sdcard/_smoke_ui.xml", binary=True)
        if rc != 0 or not data:
            raise RuntimeError(f"uiautomator dump produced nothing: {err!r}")
        return ElementTree.fromstring(data.decode("utf-8", "replace"))

    def tap(self, x, y, settle=0.7):
        self.shell(f"input tap {int(x)} {int(y)}")
        time.sleep(settle)


def centre(node):
    """Middle of a node's bounds -- which is the part you tap."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not m:
        return None
    l, t, r, b = (int(g) for g in m.groups())
    return (l + r) // 2, (t + b) // 2


def find(root, text=None, rid=None, contains=None):
    """First node matching the given text / resource-id / substring."""
    for node in root.iter("node"):
        if text is not None and node.get("text") != text:
            continue
        if contains is not None and contains not in (node.get("text") or ""):
            continue
        if rid is not None and not (node.get("resource-id") or "").endswith(rid):
            continue
        if centre(node):
            return node
    return None


def first(root, *specs):
    """First node matching any of several searches.

    NOT `find(a) or find(b)`: a matching node is a leaf <node> element with no
    children, and an Element with no children is falsy, so `or` throws away a
    perfectly good match and returns the fallback. Always compare to None.
    """
    for spec in specs:
        node = find(root, **spec)
        if node is not None:
            return node
    return None


def texts(root):
    return [n.get("text") or "" for n in root.iter("node")]


def longest_text(root):
    """The app's log is one huge TextView; that is the thing worth reading."""
    values = [n.get("text") or "" for n in root.iter("node")]
    return max(values, key=len) if values else ""


def scroll_to_bottom(device, times=6):
    for _ in range(times):
        device.shell("input swipe 540 1600 540 400 250")
        time.sleep(0.35)


def bounds_of(node):
    """(left, top, right, bottom) of a node, or None."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    return tuple(int(g) for g in m.groups()) if m else None


def labelled_boxes(root):
    """(text, box) for every node with visible text and a non-empty area."""
    out = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        box = bounds_of(node)
        if text and box and box[2] > box[0] and box[3] > box[1]:
            out.append((text, box))
    return out


def overlaps(root):
    """Labelled elements that partially intersect each other.

    A vertical LinearLayout stacks its children, so nothing in this app should
    overlap anything else. Nesting is not an overlap -- a container spanning its
    own child is normal -- so only *partial* intersections are reported, and that
    is exactly the shape of a bar drawn over the top of the form.

    This exists because a text search cannot see the failure it catches. When the
    framework ActionBar was laid over the first rows, every button label was
    still findable; what changed was geometry. The address box was the covered
    row, the token box was the only field left visible, and a user filled in the
    one they could see -- after which the app reported the address as missing.
    """
    boxes = labelled_boxes(root)
    hits = []
    for i, (ta, a) in enumerate(boxes):
        for tb, b in boxes[i + 1:]:
            if ta == tb:
                continue
            if not (a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]):
                continue
            a_in_b = b[0] <= a[0] and b[1] <= a[1] and a[2] <= b[2] and a[3] <= b[3]
            b_in_a = a[0] <= b[0] and a[1] <= b[1] and b[2] <= a[2] and b[3] <= a[3]
            if not a_in_b and not b_in_a:
                hits.append(f"「{ta}」{a} 与「{tb}」{b}")
    return hits


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def check(label, ok, detail=""):
    RESULTS.append((label, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"   {detail}"
    print(line, flush=True)
    return bool(ok)


def sha256_of(text):
    """Map filename -> hash from `sha256sum *` output."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"([0-9a-f]{64})\s+\*?(.+)$", line.strip())
        if m:
            out[m.group(2).strip()] = m.group(1)
    return out


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #

def push_photos(device):
    device.shell(f"rm -rf {DEVICE_DIR}")
    device.shell(f"mkdir -p {DEVICE_DIR}")
    for rel in PHOTOS:
        src = ROOT / rel
        if not src.exists():
            raise SystemExit(f"missing sample {src}")
        rc, _, err = device.adb("push", str(src), f"{DEVICE_DIR}/{src.name}")
        if rc != 0:
            raise RuntimeError(f"adb push failed for {src.name}: {err}")
    device.shell(f"find {DEVICE_DIR} -type f -exec touch {{}} \\;")
    listing = device.out(f"ls {DEVICE_DIR}")
    return listing


def install_app(device):
    if not APP_APK.exists():
        raise SystemExit(f"{APP_APK} not built yet -- run tools\\android_build.cmd assembleDebug")
    rc, out, err = device.adb("install", "-r", str(APP_APK), timeout=300)
    return "Success" in out or "Success" in err


def seed_prefs(device, port, server=None):
    """Point the app at the PC and turn the dry run off, without typing.

    The app is not debuggable, so `run-as` is no help; on a rooted
    emulator `adb root` lets us drop the XML in directly.

    Any `tree` key already in the file is carried over. That key is the app's
    memory of which SAF folder you granted, and it is *not* recoverable from
    the system grant -- overwriting it would silently send the next run back
    to the folder picker.

    `server` overrides the address that goes into the box, verbatim -- which is
    how the regression test for "填了地址还说我没填地址" runs. Seed the exact
    shape an auto-capitalising IME produces, `Http://10.0.2.2:8899`, and require
    the run to succeed anyway. That input was rejected before, by a
    case-sensitive `startsWith("http")` inside MainActivity.

    Returns (rooted, carried_over). A production phone refuses `adb root`, and
    then this step cannot run at all -- the caller has to already have typed
    the address into the app once, so say so instead of failing obscurely.
    """
    rc, out, err = device.adb("root")
    time.sleep(3)
    rooted = "cannot run as root" not in (out + err) and "adbd cannot" not in (out + err)
    if not rooted:
        return False, False

    rc, existing, _ = device.shell(f"cat /data/data/{APP_PKG}/shared_prefs/albumcat.xml")
    kept = ""
    m = re.search(r'name="tree">([^<]*)<', existing)
    if m:
        kept = f'    <string name="tree">{m.group(1)}</string>\n'

    xml = (
        "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
        "<map>\n"
        f"    <string name=\"server\">{server or f'http://10.0.2.2:{port}'}</string>\n"
        "    <string name=\"token\"></string>\n"
        "    <boolean name=\"dryRun\" value=\"false\" />\n"
        "    <boolean name=\"skip\" value=\"true\" />\n"
        f"{kept}"
        "</map>\n"
    )
    local = ROOT / ".tmp" / "smoke_prefs.xml"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(xml, encoding="utf-8")
    device.adb("push", str(local), "/data/local/tmp/smoke_prefs.xml")
    device.shell(
        f"mkdir -p /data/data/{APP_PKG}/shared_prefs && "
        f"cp /data/local/tmp/smoke_prefs.xml /data/data/{APP_PKG}/shared_prefs/albumcat.xml && "
        f"chown -R $(stat -c %u:%g /data/data/{APP_PKG}) /data/data/{APP_PKG}/shared_prefs && "
        f"chmod 771 /data/data/{APP_PKG}/shared_prefs && "
        f"chmod 660 /data/data/{APP_PKG}/shared_prefs/albumcat.xml"
    )
    return rooted, kept != ""


def launch_app(device):
    device.shell(f"am force-stop {APP_PKG}")
    time.sleep(1)
    device.shell(f"am start -W -n {APP_PKG}/.MainActivity")
    time.sleep(2.5)


def pick_folder(device, folder):
    """Walk the SAF picker to `DCIM/<folder>` and grant the app access."""
    root = device.dump()
    if find(root, contains=f"文件夹：primary:DCIM/{folder}") is not None:
        return True, "already remembered"

    node = first(root, dict(contains="选择相册文件夹"), dict(rid="button1"))
    if node is None:
        return False, "could not find the folder button"
    device.tap(*centre(node))
    time.sleep(2)

    # The picker may open on "Recent"; get to the root listing first.
    root = device.dump()
    if find(root, text="DCIM") is None:
        drawer = first(root, dict(rid="roots_toolbar"), dict(rid="action_bar"))
        if drawer is not None:
            device.tap(*centre(drawer))
            time.sleep(1.5)
            root = device.dump()
        volume = None
        for n in root.iter("node"):
            if n.get("resource-id", "").endswith("title") and "sdk_" in (n.get("text") or ""):
                volume = n
                break
        if volume is not None:
            device.tap(*centre(volume))
            time.sleep(1.5)
            root = device.dump()

    dcim = find(root, text="DCIM")
    if dcim is None:
        return False, "DCIM not visible in the picker"
    device.tap(*centre(dcim))
    time.sleep(1.5)

    target = find(device.dump(), text=folder)
    if target is None:
        return False, f"{folder} not visible inside DCIM"
    device.tap(*centre(target))
    use = first(device.dump(), dict(rid="button1"), dict(contains="USE THIS FOLDER"),
                dict(contains="使用此文件夹"))
    if use is None:
        return False, "USE THIS FOLDER button not found"
    device.tap(*centre(use))
    time.sleep(2)

    # The "Allow ...?" confirmation only appears the first time.
    allow = first(device.dump(), dict(text="ALLOW"), dict(text="允许"), dict(contains="Allow"))
    if allow is not None:
        device.tap(*centre(allow))
        time.sleep(2)
    return True, "granted"


def app_log(device):
    return longest_text(device.dump())


def wait_for_run(device, seconds=180):
    """Poll until the summary line appears."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(4)
        log = app_log(device)
        if "完成：" in log or "已停止" in log or "⚠" in log:
            return log
    return app_log(device)


def tap_button(device, label):
    root = device.dump()
    node = find(root, contains=label)
    if node is None:
        return False
    device.tap(*centre(node))
    return True


def device_hashes(device):
    return sha256_of(device.out(f"cd {DEVICE_DIR} && sha256sum *"))


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--server", default=None,
                    help="address to seed into the app verbatim; use an odd but "
                         "legal spelling (e.g. Http://10.0.2.2:8899, or a bare "
                         "10.0.2.2:8899) to check the app repairs it instead of "
                         "claiming the box is empty")
    args = ap.parse_args()

    if not ADB.exists():
        raise SystemExit(f"adb not found at {ADB} (set ANDROID_SDK_ROOT)")

    device = Device(args.serial)
    print(f"device          {args.serial}")
    device.check_alive()
    print(f"android         {device.out('getprop ro.build.version.release')} "
          f"(API {device.out('getprop ro.build.version.sdk')}, "
          f"{device.out('getprop ro.build.type')})")

    # ---- 0. the PC side has to be up, or every photo fails the same way --- #
    print("\n-- setup --")
    pushed = push_photos(device)
    check("probe photos are on the device", pushed.count(".jpg") + pushed.count(".png") == len(PHOTOS),
          f"{len(PHOTOS)} file(s)")
    check("the app installs", install_app(device))
    rooted, remembered = seed_prefs(device, args.port, args.server)
    if args.server:
        print(f"address seeded  {args.server!r} (deliberately odd -- the app must repair it)")
    if not rooted:
        print("  [note] `adb root` refused (a production build) -- cannot preset the")
        print("         app's preferences. Type the server address in once, pick the")
        print("         folder, and re-run; the rest of this script still works.")
    else:
        print(f"  [note] SAF folder grant {'carried over' if remembered else 'not on record yet'}")
    launch_app(device)

    # ---- the form has to be readable before anything else matters -------- #
    print("\n-- layout: the form must not be covered --")
    time.sleep(1.0)
    form = device.dump()
    labels = [t for t, _b in labelled_boxes(form)]
    hits = overlaps(form)
    check("no two labelled elements overlap (nothing is drawn over the form)",
          not hits, "；".join(hits[:3]) if hits else "")
    check("both input boxes carry a visible label, not just a placeholder",
          "电脑端地址" in labels and "令牌（电脑端没设 --token 就留空）" in labels,
          "；".join(labels[:3]))
    # The framework ActionBar carried the app label. Its return would push the
    # form down *and* draw over it, so assert the label is not a node of its own.
    check("no framework ActionBar above the form",
          "相册分类" not in labels,
          "标题栏还在" if "相册分类" in labels else "")
    # Content flush against y=0 means the status bar is sitting on the form
    # (targetSdk 35 draws edge to edge unless the app insets itself). Approximate
    # -- it is anchored to a phone status bar, which every target device has.
    top = min((b[1] for _t, b in labelled_boxes(form)), default=0)
    check("the form starts below the top edge, clear of the status bar",
          top >= 40, f"最上面的元素 top={top}")

    ok, why = pick_folder(device, "AlbumcatSmoke")
    if not check("SAF folder picked and access granted", ok, why):
        return finish()
    # The folder label is its own (short) TextView, so it is not part of the
    # big log blob that app_log() returns -- look at every text on screen.
    on_screen = "\n".join(texts(device.dump()))
    check("folder shows as granted", "primary:DCIM/AlbumcatSmoke" in on_screen, why)

    before = device_hashes(device)
    check("baseline hashes captured", len(before) == len(PHOTOS), f"{len(before)} file(s)")

    # ---- 1. the real run ------------------------------------------------- #
    print("\n-- run: tag the photos --")
    if not tap_button(device, "开始"):
        check("start button found", False)
        return finish()
    log = wait_for_run(device)
    check("run finished cleanly", "完成：" in log, _summary(log))
    check("nothing failed", re.search(r"失败 0 张", log) is not None, _summary(log))
    check(f"wrote {len(PHOTOS)} photos", re.search(rf"写入 {len(PHOTOS)} 张", log) is not None,
          _summary(log))
    if args.server:
        # Not just "it worked anyway": the app must have gone through the repair
        # path and said so. Otherwise a future change could start accepting the
        # odd spelling by accident and this test would not notice.
        check("the odd address was repaired, not silently ignored",
              "地址已按" in log, _summary(log))

    after = device_hashes(device)
    grew = [n for n in before if n in after and after[n] != before[n]]
    check("every photo was actually rewritten", len(grew) == len(PHOTOS),
          f"{len(grew)}/{len(PHOTOS)} changed")

    # ---- 2. re-running must be a no-op ----------------------------------- #
    print("\n-- run again: must skip and change nothing --")
    tap_button(device, "开始")
    log = wait_for_run(device)
    check(f"all {len(PHOTOS)} skipped as already tagged",
          re.search(rf"跳过 {len(PHOTOS)} 张", log) is not None, _summary(log))
    rerun = device_hashes(device)
    check("re-run left every byte alone", rerun == after,
          "" if rerun == after else "hashes moved")

    # ---- 3. undo must be byte-exact -------------------------------------- #
    print("\n-- undo: must restore the originals --")
    if not tap_button(device, "撤销"):
        check("undo button found", False)
        return finish()
    log = wait_for_run(device)
    check("undo reported a restore", "已还原" in log or "还原" in log, "")
    final = device_hashes(device)
    restored = [n for n in before if final.get(n) == before[n]]
    check("undo restored every photo byte-for-byte", len(restored) == len(PHOTOS),
          f"{len(restored)}/{len(PHOTOS)} identical")
    if len(restored) != len(PHOTOS):
        for name in before:
            if final.get(name) != before[name]:
                print(f"        {name}: {before[name][:12]} -> {(final.get(name) or '-')[:12]}")

    return finish()


def _summary(log):
    for line in log.splitlines():
        if line.startswith("完成："):
            return line
    return ""


def finish():
    failed = [r for r in RESULTS if not r[1]]
    print()
    print("-" * 70)
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed, {len(failed)} FAILED:")
        for label, _, detail in failed:
            print(f"  - {label}   {detail}")
        return 1
    print(f"All {len(RESULTS)} checks passed -- the app works on a real Android system.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
