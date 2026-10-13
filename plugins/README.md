# Plugins

A plugin is a directory under `plugins/` containing a `plugin.json`. It is any
extra recogniser you want to bolt onto the WD14 pipeline — most importantly a
classifier for *your* favourite franchise, which is how you get precise
character recognition instead of whatever the general model happens to score.

    plugins/
      example-flat-image/
        plugin.json
        plugin.py

The host discovers plugins at startup, feeds each one every image on its way
through the pipeline, and merges whatever comes back into the same `TagSet`
WD14 wrote into. The rules engine then treats plugin predictions exactly like
model predictions, except each one carries its plugin id as its `source`, so
you can write rules that only fire on plugin output.

Check what is loaded:

    tools\albumcat.cmd plugins

## The five kinds

| `kind`    | Needs                        | Use it for                                                    |
|-----------|------------------------------|---------------------------------------------------------------|
| `feature` | `model` (an ONNX head) + `labels` | **The precise-character path.** A small classifier on top of WD14's own image embedding (768-d for ViT-B/16). Cheap: no image decode, no extra inference pass. |
| `python`  | `plugin.py`                  | Arbitrary Python. OCR, perceptual hashing, face matching, reading a sidecar file. |
| `image`   | `plugin.py`                  | Alias for `python`; both load `plugin.py`. Use it when the name documents your intent. |
| `remote`  | `endpoint`                   | Forward the preview image to an HTTP service elsewhere on your LAN. |
| `rule`    | `rules` (a `rules.json`)     | No model and no code — just extra rules for the rule engine. |

`feature` is the one that matters for "recognise this specific IP's characters
properly". A linear head trained on a few hundred of your own corrected images
will beat WD14's own character labels on that franchise by a wide margin, and
because it reads the embedding that WD14 already computed it adds almost no time
per image.

⚠ The embedding only exists if the model file exposes it. Upstream's answer is
`third_party/wd14-API-onnx/split_model.py`, which carves the network into a
1.2 GB feature extractor plus a 43 MB classifier. **You do not need that here.**
`tools/add_feature_output.py` declares the embedding as one extra graph output of
the *existing* model, so a single session returns both tag confidences and the
embedding -- one 373 MB file instead of two totalling 1.25 GB.

    python tools\add_feature_output.py

Run it once; `albumcat` picks up `model_features.onnx` automatically. It verifies
the result by rebuilding the classifier's own output from the embedding and
refusing to keep the file if the two disagree. `albumcat doctor` shows whether
embeddings are available, and `albumcat model` prints their real width.

## `plugin.json` fields

| field        | default     | meaning                                                       |
|--------------|-------------|---------------------------------------------------------------|
| `id`         | *required*  | also the directory name; becomes the prediction `source`      |
| `kind`       | *required*  | one of the five above                                         |
| `target`     | `character` | which bucket outputs land in: `character`, `tag` or `rating`  |
| `priority`   | `100`       | lower runs first; plugins run after WD14                      |
| `threshold`  | `0.5`       | predictions below this are dropped by the host                |
| `version`    | `0.0.0`     | free text                                                     |
| `name`       | = `id`      | human-readable name shown by `albumcat plugins`               |
| `description`| `""`        | free text                                                     |
| `author`     | `""`        | free text                                                     |
| `labels`     | `[]`        | `feature` only: output labels, index-aligned with the head    |
| `input_dim`  | `0`         | `feature` only: expected head input width; `0` skips the check. Read the real value with `albumcat model` (768 for `wd-v1-4-vit-tagger-v2`) |
| `model`      | `""`        | `feature` only: path to the `.onnx`, relative to the plugin   |
| `sha256`     | `""`        | `feature` only: optional integrity check on `model`           |
| `rules`      | `""`        | `rule` only: path to `rules.json` (defaults to `rules.json`)  |
| `endpoint`   | `""`        | `remote` only: URL to POST the image to                       |
| `headers`    | `{}`        | `remote` only: extra HTTP headers                             |
| `enabled`    | `true`      | set false to load but never run                               |

Unknown keys are kept in `extra` rather than rejected, so you can annotate your
own manifests freely.

## Writing a `feature` head

This is the whole recipe for a franchise-specific character classifier.

1. **Make embeddings available** (once per model):

       python tools\add_feature_output.py

   That writes `model_features.onnx` beside `model.onnx` and verifies it.

2. **Collect labelled embeddings.** Run the pipeline over the images you care
   about, then correct the mistakes by hand. Every correction is an
   `(embedding, correct label)` pair. A few hundred per character is plenty for a
   linear head.

3. **Train and export** to `plugins/<id>/head.onnx` with input shape `[N, 768]`
   (whatever `albumcat model` reports) and one output per label, in the same
   order as your `labels` list. Raw logits are fine — the host applies a sigmoid
   when it sees values outside `[0, 1]`.

4. **Register it:**

   ```json
   {
     "id": "my-franchise",
     "kind": "feature",
     "name": "My Franchise characters",
     "target": "character",
     "priority": 10,
     "threshold": 0.6,
     "labels": ["Character A", "Character B", "not_this_franchise"],
     "input_dim": 768,
     "model": "head.onnx",
     "sha256": "<optional integrity check>"
   }
   ```

   Give it a `priority` below 100 so it is resolved before WD14's own
   character guesses, and a `threshold` you tuned on your own data.

5. **Hot-reload** by restarting the pipeline; there is no state to migrate.

Include a `not_this_franchise` label. It gives the head an explicit way to say
"none of the above" instead of being forced to pick a character, which is the
single biggest source of false positives in a multi-class head.

## Writing a `rule` plugin

No code at all — a `rules.json` in the standard rule format. Rule ids are
prefixed with the plugin id automatically, so they cannot collide with your
main rules file.

```json
{
  "rules": [
    {
      "id": "quarantine-flat",
      "priority": 5,
      "description": "Keep obvious junk out of the character folders.",
      "when": { "key": "低质量图片", "score_gte": 0.8 },
      "then": { "kind": "tag", "path": ["待清理"], "reason": "flat image" }
    }
  ]
}
```

`when` accepts any of `category`, `key`, `key_regex`, `score_gte`, `score_lt`,
`series`, `rank_lt` (all ANDed). `then` is one of:

* `{"kind": "tag", "path": ["角色", "初音未来"]}` — publish a hierarchical tag
* `{"kind": "drop"}` — discard the matched prediction
* `{"kind": "star", "value": 5}` — set the Immich star rating
* `{"kind": "review"}` — flag the image for human review

`priority` is lower-first. Rules in `rules.json` default to 10, which puts them
ahead of the built-in character/rating/general logic, so a `drop` here really
does suppress a tag that would otherwise have been written.
