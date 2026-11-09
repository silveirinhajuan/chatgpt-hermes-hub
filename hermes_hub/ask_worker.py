from __future__ import annotations

import io
import json
import os
import sys


def main() -> int:
    hermes_root = os.environ.get("HERMES_ROOT") or os.path.join(
        os.path.expanduser("~"), ".hermes", "hermes-agent"
    )
    sys.path.insert(0, hermes_root)
    sys._hermes_pin_default_home = True
    os.environ.setdefault("HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes"))
    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("VIRTUAL_ENV", None)

    try:
        import hermes_bootstrap  # type: ignore  # noqa: F401
    except Exception as exc:
        sys.stderr.write("hermes_bootstrap import failed: %s\n" % exc)

    try:
        import hermes_cli.plugins as _plugins
        _plugins.discover_plugins = lambda *a, **kw: None
    except Exception:
        pass
    try:
        import hermes_cli.mcp_startup as _mcp
        _mcp.ensure_mcp_discovery_before_agent_build = lambda *a, **kw: None
    except Exception:
        pass

    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        payload = {"question": raw}

    question = (payload.get("question") or "").strip()
    model = payload.get("model")
    provider = payload.get("provider")
    reasoning = payload.get("reasoning")

    if not question:
        print(json.dumps({"ok": False, "error": "pergunta vazia"}), flush=True)
        return 2

    try:
        from toolsets import create_custom_toolset
        create_custom_toolset("__hub_ask_no_tools__", "Ferramentas desabilitadas para Q&A do hub")
    except Exception as exc:
        sys.stderr.write("create_custom_toolset failed: %s\n" % exc)

    bridge_instruction = (
        "PONTE DE Q&A SOMENTE TEXTO — VOCÊ NÃO TEM FERRAMENTAS.\n"
        "Você está respondendo a uma pergunta encaminhada por outra IA.\n"
        "Não há ferramentas disponíveis: não tente executar comandos, ler "
        "arquivos, acessar a web ou chamar MCP. Responda diretamente em texto, "
        "usando apenas o que você sabe. Nunca emita chamadas de ferramenta, "
        "XML de tool_call, marcadores como <longcat_tool_call>, nem blocos de "
        "código simulando ferramentas. Se a pergunta exigir uma ação que você "
        "não pode executar, explique isso em vez de simular a ação."
    )
    full_prompt = bridge_instruction + "\n\nPergunta: " + question

    os.environ.setdefault("HERMES_IGNORE_RULES", "1")

    try:
        import contextlib
        from hermes_cli.oneshot import run_oneshot

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_oneshot(
                full_prompt,
                model=model,
                provider=provider,
                toolsets=["__hub_ask_no_tools__"],
                reasoning=reasoning,
            )
        answer = buf.getvalue().strip()
        out = {"ok": rc == 0, "rc": rc, "answer": answer}
        if not out["ok"] and not answer:
            out["error"] = "hermes oneshot rc=%d" % rc
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return 0 if rc == 0 else 1
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
