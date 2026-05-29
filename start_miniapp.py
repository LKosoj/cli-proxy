
import asyncio
import logging
import sys
from bot import BotApp
from config import load_config


async def main():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)

    print("Loading config...")
    config = load_config("config.yaml")
    config.miniapp.enabled = True

    print("Initializing BotApp...")
    bot_app = BotApp(config)

    print("Starting shared ingress...")
    # Explicitly set host/port if not set
    if not hasattr(bot_app, "shared_http_ingress"):
        from app.services.shared_http_ingress import SharedHttpIngress

        bot_app.shared_http_ingress = SharedHttpIngress(host="127.0.0.1", port=8088)

    await bot_app.shared_http_ingress.start()

    print("MiniApp started at http://127.0.0.1:8088/cli-proxy/")

    # Keep alive
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
