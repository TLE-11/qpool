# HiRoute decision policy

Source: https://github.com/higress-group/HiRoute
Revision: 52d2b9b0f131ef423930f8c2834683b27696ad2d
Files: decision-extensions/extensions/jev-decider/jev_decider/{decision,protocol}.py
License: Apache-2.0 (bundled as LICENSE). No upstream NOTICE file was present.

The two Python modules are unmodified. qpool's transport, context projection,
Responses scoring prompt and configuration are separate in qp/decisions.py.
Only smart_saving task-degree assessment is currently used. No preceding-stage
competence assessment is inferred from incomplete Codex execution history.
A general Responses model's self-reported probabilities are not the specialized
Jev service's calibrated scores and do not establish equal classification quality.
