import sys
import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).parent / "settings.json"
api_key = None
base_url = "https://api.deepseek.com"

if SETTINGS_PATH.exists():
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        api_key = data.get("api_keys", {}).get("deepseek")
        deepseek_cfg = data.get("providers", {}).get("deepseek", {})
        if deepseek_cfg.get("base_url"):
            base_url = deepseek_cfg["base_url"]
    except Exception as e:
        print(f"Warning: Could not read settings.json: {e}")

if not api_key:
    raise SystemExit("Configure a DeepSeek API key in Connections before starting this session.")

from openai import OpenAI

client = OpenAI(api_key=api_key, base_url=base_url)

def main():
    model = "deepseek-chat"
    if len(sys.argv) > 1:
        model = sys.argv[1]

    print("=" * 60)
    print(" DeepSeek Official Interactive Session")
    print(f" Base URL : {base_url}")
    print(f" Model    : {model}")
    print(" Commands :")
    print("   exit / quit        - Exit session")
    print("   /model <name>      - Switch model (e.g. deepseek-reasoner)")
    print("   /clear             - Clear chat history")
    print("=" * 60)
    print()

    messages = [
        {"role": "system", "content": "You are a helpful, expert AI assistant."}
    ]

    while True:
        try:
            user_input = input("You > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting session. Goodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", ":q"):
            print("Session closed.")
            break

        if user_input.startswith("/model "):
            model = user_input.split(" ", 1)[1].strip()
            print(f"[Model switched to: {model}]\n")
            continue

        if user_input.lower() == "/clear":
            messages = [{"role": "system", "content": "You are a helpful, expert AI assistant."}]
            print("[Conversation history cleared]\n")
            continue

        messages.append({"role": "user", "content": user_input})
        print(f"\nDeepSeek ({model}) > ", end="", flush=True)

        try:
            response_stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True
            )

            full_content = ""
            reasoning_content = ""
            in_reasoning = False

            for chunk in response_stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                
                # Check for reasoning content (deepseek-reasoner / R1)
                r_text = getattr(delta, "reasoning_content", None)
                if r_text:
                    if not in_reasoning:
                        in_reasoning = True
                        print("\n[Thinking: ", end="", flush=True)
                    print(r_text, end="", flush=True)
                    reasoning_content += r_text

                c_text = delta.content or ""
                if c_text:
                    if in_reasoning:
                        in_reasoning = False
                        print("]\n\n", end="", flush=True)
                    print(c_text, end="", flush=True)
                    full_content += c_text

            if in_reasoning:
                print("]")
            print("\n")

            messages.append({"role": "assistant", "content": full_content})

        except Exception as e:
            print(f"\n[Error: {e}]\n")
            messages.pop()

if __name__ == "__main__":
    main()

