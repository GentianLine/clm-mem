import json, sys, time
from clm_mem import start_supervisor

sup = start_supervisor(
    public_port=8717,
    served_model="mock-model",
    backend_mode="mock",
    systemone_mode="mock",
    harness_kwargs={"workdir": "/tmp/clm_ui_workdir", "max_context_tokens": 4096},
    timeout=30,
)
print(json.dumps({"ui": sup.ui_url if hasattr(sup, "ui_url") else None,
                  "base": sup.base_url}), flush=True)
open("/tmp/clm_ui_url.json", "w").write(json.dumps(
    {"ui": getattr(sup, "ui_url", None), "base": sup.base_url}))
while True:
    time.sleep(5)
