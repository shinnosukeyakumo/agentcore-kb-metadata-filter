"""Policy（Cedar）で userContext の偽装が止まるか。トークンは token exchange で取る（aud = kbmf-gateway）。

interceptor なし（kbmf-gw-policy のターゲット kb）
P1 spoof   : user-b のトークンで userContext に user-a を書く → 拒否される
P2 missing : userContext を書かない → 拒否される
P3 match   : userContext に自分（user-b）を書く → 通る

interceptor あり（kbmf-gw のターゲット locked）
P4 spoof   : P1 と同じ偽装 → interceptor が user-b に書き換えてから Cedar が評価するので通り、user-b の文書だけが返る
"""

import importlib
import json

from common import USER_A, USER_B, gateway_token, load_state, save_result

gwtest = importlib.import_module("11_test_gateway")


def main() -> None:
    state = load_state()
    url = state["policy"]["url"]
    tb = gateway_token(state, USER_B)
    out = {
        "P1_spoof": gwtest.call(url, tb, "kb", "hiring", {"userContext": {"userId": USER_A}}),
        "P2_missing": gwtest.call(url, tb, "kb", "hiring"),
        "P3_match": gwtest.call(url, tb, "kb", "hiring", {"userContext": {"userId": USER_B}}),
        "P4_spoof_with_interceptor": gwtest.call(state["gateway"]["url"], tb, "locked", "hiring",
                                                 {"userContext": {"userId": USER_A}}),
    }
    print(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"saved {save_result('13_policy', out)}")


if __name__ == "__main__":
    main()
