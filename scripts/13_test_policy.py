"""Policy（Cedar）付きの Gateway（kbmf-gw-policy）で、userContext の偽装が止まるか。

P1 spoof   : user-b のトークンで userContext に user-a を書く → 拒否される
P2 missing : userContext を書かない → 拒否される
P3 match   : userContext に自分（user-b）を書く → 通る
"""

import importlib
import json

from common import USER_A, USER_B, load_state, save_result, sign_in

gwtest = importlib.import_module("11_test_gateway")


def main() -> None:
    state = load_state()
    url = state["policy"]["url"]
    tb = sign_in(state, USER_B)
    out = {
        "P1_spoof": gwtest.call(url, tb, "kb", "hiring", {"userContext": {"userId": USER_A}}),
        "P2_missing": gwtest.call(url, tb, "kb", "hiring"),
        "P3_match": gwtest.call(url, tb, "kb", "hiring", {"userContext": {"userId": USER_B}}),
    }
    print(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"saved {save_result('13_policy', out)}")


if __name__ == "__main__":
    main()
