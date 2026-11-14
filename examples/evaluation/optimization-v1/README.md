# Optimization v1 fixed code tasks

Three development tasks are separate from six formal low/medium/high Python and
TypeScript tasks. The manifest binds every workspace, reference and hidden file.
Only workspace/ is copied to Agent workspaces. reference/ and hidden/ are host
evaluation material and must never be mounted into an Agent execution workspace.

All tests and reference candidates execute inside the hardened tool container.
Each acceptance reports five requirement groups; functional success requires all
five and exit zero. TypeScript additionally requires the pinned offline compiler.
The original defective candidates intentionally fail acceptance.

Do not change these files once the formal trial begins. Any corrections require
a new dataset version and must retain all prior results. File/LOC counts are not
difficulty scores: dependency and API/transaction boundaries define the levels.

The high tier is relative to these bounded fixtures. It exercises transactions,
compatibility and multi-module integration; it is not a large-repository benchmark.

## Running the authorized campaign

Use `scripts/m4_evaluate.py` with a private schema-2 plan. Required fields are
`api_url`, `strong_model_id`, `economy_model_id`, `implementation_id` (fixed app
image/version), absolute `workspace_root`, `output_root`, `session_cookie_file`,
`authorization_id: m4-optimization-20261109`, and
`budget_ledger: G:/harness/.omniharness/m4-optimization/budget.sqlite3`.
The sandbox object contains explicit `endpoint`, private `key_file`, fixed
`image`, Linux `snapshot_root` and, for this Windows deployment, `wsl_distro`.
The session cookie file contains only the local session value, never a model key.

Without `--execute` the runner only describes the 105-trial schedule. Fixed seed
43 creates contiguous task/repetition blocks with all five conditions. The
schedule is 15 development, 30 first formal, 60 further formal trials. Real model
capabilities must already be verified; the runner never silently probes a model.

```
python scripts/m4_evaluate.py --plan PRIVATE_PLAN.json
python scripts/m4_evaluate.py --plan PRIVATE_PLAN.json --calibrate-only
python scripts/m4_evaluate.py --plan PRIVATE_PLAN.json --execute --through development
python scripts/m4_evaluate.py --plan PRIVATE_PLAN.json --execute --through first --resume REPORT_DIRECTORY --freeze-formal
python scripts/m4_evaluate.py --plan PRIVATE_PLAN.json --execute --through all --resume REPORT_DIRECTORY
```

The one explicit formal freeze is allowed only after all 15 development records,
before any formal trial. It records implementation, models, context, dataset,
quote snapshot, sandbox and seed. Once formal work starts these cannot change.
Do not edit a completed trial or replace failed trials. A resumed driver reads
the original Run after lost dispatch responses; it does not resend a paid request.
If the outcome cannot be recovered it stops with the full allowance reserved.

Every Run reserves 50 calls from the same durable 1,500-call campaign ledger used
by capability probes and repair smoke tests. Fewer than 50 unreserved calls stops
the batch in its fixed order. This is a request budget, not a currency commitment.

Reports keep workflow completion separate from code acceptance, include failed
attempt costs, separate summed concurrent model time from wall-time union, and
only compare speed ratios when both paired trials resolved the task. Formal
summaries exclude development records. Public highest-band costs are estimates
before account group/discounts; billed cost remains unknown without billing data.
