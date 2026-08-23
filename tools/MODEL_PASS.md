# Model pass — GB10 / playa séance

Paper ranking + inference-params review for the Terrible Turtle Oracle
on `spark-239e` (aarch64 Ubuntu, GB10, 121 GB unified, Ollama
`NUM_PARALLEL=1`). Live numbers come from `tools/bench_models.py` once
the Spark is back; this file is the argument that decides **what to
pull** and **what the bench is allowed to change**.

Constraint: a seeker is standing in a tent. Target 30–60s wall for a
full séance (~4 LLM calls: follow-up, weave, echoes, seal). Two
stations may overlap. Ollama serialises on this GPU — two concurrent
séances means each call waits on the other. Reasoning models are
wrong: they spend the latency budget inside `<think>` and we already
strip that.

Measured on this box already (do not re-litigate):

| model | séance wall | JSON | note |
|---|---|---|---|
| **qwen3:30b-a3b** | 8.7–10.5s solo | holds | current default; ~85 tok/s |
| qwen3.5:35b-a3b | 12–30s | holds | slower, more abstract |
| qwen3.8:27b | 38–40s | holds | best voice, no headroom |
| gemma4:26b | ~108s | 0/4 | timed out, silent fallback |
| gpt-oss:20b | n/a | empty | structured-output fail |

## Ranked pull list

Pull in this order. Stop if disk or time runs out — rank 0 stays live.

| rank | `ollama pull` | ~disk | ~active params | expect on GB10 | verdict |
|---|---|---|---|---|---|
| 0 | `qwen3:30b-a3b` (already on box) | ~19 GB | 3.3B of 30B MoE | 70–90 tok/s, 9–15s séance even with a second seeker queued | **keep as default** until the harness falsifies it |
| 1 | `mistral-small:24b` | ~15 GB | 24B dense | 25–40 tok/s; séance maybe 20–35s | **pull**. Not a reasoner. Spoken prose, Apache. The quality check against the MoE. |
| 2 | `qwen3:32b` | ~20 GB | 32B dense | 10–20 tok/s; weave alone may eat the 60s guard with 2 seekers | **pull as ceiling**, not as default. Same family as current, all experts on. |
| 3 | `gemma3:27b` | ~17 GB | 27B dense | 20–35 tok/s | **pull once**. Not gemma4:26b. One run, then drop if JSON or voice is off. |
| — | `llama3.3:70b` | ~43 GB | 70B dense | 12–25 tok/s, 4 JSON calls serialised → 60–90s | **do not pull** for this workload. Fits in 121 GB; fails the standing-seeker budget. |

```
ollama pull mistral-small:24b
ollama pull qwen3:32b
ollama pull gemma3:27b
```

Two concurrent séances: only rank 0 has a measured safety margin under
`T_LONG=60`. Anything dense has to beat 30b-a3b on *voice* by enough
to pay for the extra wait, or it loses.

## What the app actually sends today

`app/oracle/llm.py` `LLM.generate`:

```
POST /api/generate
  stream: false
  keep_alive: -1
  think: false          # if model name starts with qwen3 / deepseek / gpt-oss / magistral
  format: json          # if as_json
  options:
    temperature: 0.75
```

Not set: `num_predict`, `num_ctx`, `top_p`, `repeat_penalty`, `num_gpu`,
`num_thread`. Call timeouts: `T_SHORT=45` (follow-up, echoes),
`T_LONG=60` (weave, refine, seal). Unit file: `ORACLE_MODEL=qwen3:30b-a3b`,
`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=-1`.
Code default is still `qwen2.5` if the env is missing.

A séance is four generate calls on the happy path (follow-up one-liner,
weave JSON ~130–170 words + quest, echoes JSON 3 lines, seal JSON 3
moves). Refine is a fifth, only on "hear me further".

## What it should set (do not ship without a bench row)

| knob | now | recommend | why |
|---|---|---|---|
| temperature | 0.75 all calls | keep 0.75 on one-liners; **0.5 on JSON** (weave/echoes/seal/refine) | schema + "exactly one leave" fail at 0.75; spoken follow-up wants the bite |
| num_predict | unset (runaway) | follow-up 80; echoes 180; weave 700; seal 500 | a stuck decode currently sits until the 45/60s HTTP timeout, then the seeker gets a template and `/api/health` only notices if we count it |
| num_ctx | ollama default (often 2048–4096) | **8192** | weave prompt is the three cards + lore + seeker share + the long binding; 2048 will silently truncate the safety covenant |
| think | false on qwen3* | keep; add `gemma3` / `qwen3.5` prefixes to `NO_THINK` if those tags start thinking | |
| keep_alive | -1 | keep | reload is a 20s tax on the next seeker |
| stream | false | keep in production | TTFT is a bench metric, not a UX one — the kiosk waits for the full JSON |
| NUM_PARALLEL | 1 | keep | measured: 4-way is slower on GB10 (38.8s vs 30.6s) |
| top_p / repeat_penalty | unset | leave unset until a model loops | one knob at a time |

The harness already sends the recommended options on the bench path
(`--prod-options` to replay exactly what production sends). Compare
the two columns on the winning model before folding anything into
`llm.py`.

## How to run the harness on the Spark

From the repo, after the pulls:

```
PYTHONPATH=app python3 tools/bench_models.py --self-test
PYTHONPATH=app python3 tools/bench_models.py \
  --host http://127.0.0.1:11434 \
  --models qwen3:30b-a3b,mistral-small:24b,qwen3:32b,gemma3:27b
```

`--prod-options` uses production's `{temperature: 0.75}` only.
Default is the recommended set above.

Output: per model × 3 seekers × 4 calls: TTFT, tok/s, wall, JSON
parse, reading word-count, banned-word hits. Rank 0 is the control;
a challenger has to beat it on voice *and* stay inside 60s with a
second call queued (the script prints a "2-seeker tax" = 2× weave
wall).
