"""TEST-ONLY fault injection switch (admin CLI; there is no public API for it).

  python scripts/faults.py show
  python scripts/faults.py set '{"worker": {"fail_rows": [7]}}'
  python scripts/faults.py clear      # always run this after an experiment

Only works when the stack was deployed with -c faultInjection=true.
"""

import json
import sys
import time

from cb import aws, outputs


def main():
    param = outputs().get("FaultsParam")
    if not param:
        sys.exit("Fault injection is disabled in this deployment (no FaultsParam output).")
    ssm = aws("ssm")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "show":
        print(ssm.get_parameter(Name=param)["Parameter"]["Value"])
        return
    value = "{}" if cmd == "clear" else json.dumps(json.loads(sys.argv[2]))
    ssm.put_parameter(Name=param, Value=value, Type="String", Overwrite=True)
    print(f"faults = {value}  (functions pick it up within ~5 s)")
    time.sleep(6)


if __name__ == "__main__":
    main()
