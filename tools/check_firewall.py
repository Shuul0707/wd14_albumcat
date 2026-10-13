"""Exercise the firewall preflight, including cases this PC cannot show."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albumcat import firewall  # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r}, want {want!r}")


print("=== live machine ===")
print("executable      :", sys.executable)
print("active profiles :", firewall.active_profiles())
ok, msg = firewall.preflight(8899)
print("preflight       :", ok)
print("  " + msg.replace("\n", "\n  "))
print()
progs = [p for p in firewall.local_programs() if "python" in p.lower()]
print("python programs that already have inbound rules:")
for p in progs:
    print("   ", p)
print()

print("=== parser, against synthetic netsh text (real layout) ===")
# Reproduces the detail that broke the first version: netsh rules off the
# dashes under each "Rule Name:" header AND between records.
TEXT = """
Rule Name:                            python.exe
----------------------------------------------------------------------
Description:                          python.exe
Enabled:                              Yes
Direction:                            In
Profiles:                             Private,Public
Protocol:                             UDP
LocalPort:                            Any
Program:                              C:\\other\\python.exe
Action:                               Allow

Rule Name:                            albumcat serve
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private,Domain
Protocol:                             TCP
LocalPort:                            8899
Program:                              C:\\venv\\Scripts\\python.exe
Action:                               Allow

Rule Name:                            disabled thing
----------------------------------------------------------------------
Enabled:                              No
Direction:                            In
Profiles:                             Private
Protocol:                             TCP
LocalPort:                            8899
Program:                              C:\\venv\\Scripts\\python.exe
Action:                               Allow

Rule Name:                            port-only rule
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Public
Protocol:                             TCP
LocalPort:                            8000-8100,8899
Action:                               Allow

Rule Name:                            System rule
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private
Protocol:                             TCP
LocalPort:                            8899
Program:                              System
Action:                               Allow
"""
rules = firewall.parse_rules(TEXT)
check("parsed rule count", len(rules), 5)
check("names are in order", [r["Rule Name"] for r in rules],
      ["python.exe", "albumcat serve", "disabled thing", "port-only rule", "System rule"])
check("Program lands on the right rule", rules[1].get("Program"), "C:\\venv\\Scripts\\python.exe")
check("first rule keeps its own Program", rules[0].get("Program"), "C:\\other\\python.exe")
check("LocalPort lands on the right rule", rules[1].get("LocalPort"), "8899")

EXE = "C:\\venv\\Scripts\\python.exe"
PRIV = ["Private"]
BOTH = ["Private", "Public"]
check("covers: program + port", firewall.coverage(rules, EXE, 8899, profiles=PRIV),
      ["albumcat serve"])
check("disabled rule never counts", [r for r in firewall.coverage(rules, EXE, 8899, profiles=BOTH)
                                     if r == "disabled thing"], [])
check("System-scoped rule never counts", [r for r in firewall.coverage(rules, EXE, 8899, profiles=PRIV)
                                          if r == "System rule"], [])
check("unrelated port", firewall.coverage(rules, EXE, 9999, profiles=BOTH), [])
check("Public-only rule not seen from Private", firewall.coverage(rules, EXE, 8899, profiles=["Domain"]),
      ["albumcat serve"])
check("active profile set is honoured", firewall.coverage(rules, EXE, 8899, profiles=BOTH),
      ["albumcat serve", "port-only rule"])
check("program-agnostic rule counts", firewall.coverage(rules, "C:\\x\\y.exe", 8050, profiles=["Public"]),
      ["port-only rule"])
check("range lower bound", firewall.coverage(rules, "C:\\x\\y.exe", 8000, profiles=["Public"]),
      ["port-only rule"])
check("range outside", firewall.coverage(rules, "C:\\x\\y.exe", 8101, profiles=["Public"]), [])
check("UDP-only rule is not TCP", firewall.coverage(rules, EXE, 53, profiles=PRIV), [])
check("wrong program", firewall.coverage(rules, "C:\\elsewhere\\python.exe", 8899, profiles=PRIV), [])

print()
print("=== the same rules as netsh actually prints them on a Chinese Windows ===")
CN = """
规则名称:                             python.exe
----------------------------------------------------------------------
描述:                                 python.exe
已启用:                               是
方向:                                 入
配置文件:                             专用,公用
协议:                                 UDP
本地端口:                             任何
程序:                                 C:\\uv\\python.exe
操作:                                 允许

规则名称:                             albumcat serve
----------------------------------------------------------------------
已启用:                               是
方向:                                 入
配置文件:                             专用,公用
协议:                                 TCP
本地端口:                             8899
程序:                                 C:\\venv\\Scripts\\python.exe
操作:                                 允许
"""
cn_rules = firewall.parse_rules(CN)
check("Chinese: rule count", len(cn_rules), 2)
check("Chinese: values are read", cn_rules[1].get("Program"), "C:\\venv\\Scripts\\python.exe")
check("Chinese: UDP rule excluded", firewall.coverage(cn_rules, EXE, 8899, profiles=BOTH),
      ["albumcat serve"])
check("Chinese: foreign python still not us",
      firewall.coverage(cn_rules, "C:\\other\\python.exe", 8899, profiles=BOTH), [])
check("Chinese: profile field is understood",
      firewall.coverage(cn_rules, EXE, 8899, profiles=["Domain"]), [])

print()
print("=== profile parsing, both languages ===")
PROFILE_TEXT = """
Private Profile:
----------------------------------------------------------------------
Radmin VPN

Public Profile:
----------------------------------------------------------------------
i-HDU
Ok.
"""
CN_PROFILE_TEXT = """
专用配置文件:
----------------------------------------------------------------------
Radmin VPN

公用配置文件:
----------------------------------------------------------------------
i-HDU
确定。
"""
check("active profiles parsed (en)", firewall.parse_profiles(PROFILE_TEXT), ["Private", "Public"])
check("active profiles parsed (zh)", firewall.parse_profiles(CN_PROFILE_TEXT), ["Private", "Public"])
check("incidental Profile: line ignored",
      firewall.parse_profiles("描述: something Profile:\n"), [])

cmd = firewall.add_rule_command(8899, EXE)
check("command scopes the program", cmd.endswith(f'program="{EXE}"'), True)
check("command does not guess a profile", "profile=any" in cmd, True)

print()
if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("firewall preflight: all checks passed")
