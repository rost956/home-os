# Checkpoint A1 — Home AI conservation

Home AI development is temporarily frozen after local-runtime experiments. The
always-on hardware does not yet provide an acceptable quality/latency balance.

All AI code, models, migrations, tests, runtime scripts, and documentation are
preserved. `HOME_AI_ENABLED=false` is now the feature gate: it hides AI UI and
does not register AI routes, so Home OS starts normally without llama.cpp. To
resume AI work, set `HOME_AI_ENABLED=true` and configure the existing `AI_*`
runtime values.

Checkpoint A1 is complete. The next checkpoint is A2: user settings foundation,
appearance/navigation preferences, and financial-period start.
