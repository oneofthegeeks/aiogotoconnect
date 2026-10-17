"""Live smoke test: place a real call and play a WAV file.

    GOTO_SIP_USERNAME=... GOTO_SIP_PASSWORD=... python examples/make_call.py \
        --to +15551234567 --wav message.wav [--transport tls]

The WAV should be 16-bit PCM mono; it is converted to G.711 before sending.
"""
import argparse
import asyncio
import os
import sys

from gotoconnect import GoToVoice


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", required=True, help="number to call, e.g. +15551234567")
    parser.add_argument("--wav", required=True, help="path to the WAV file to play")
    parser.add_argument("--server", default="sip.goto.com")
    parser.add_argument("--proxy", help="outbound proxy host (connection target; --server stays in URIs)")
    parser.add_argument("--transport", choices=["udp", "tls"], default="udp")
    args = parser.parse_args()

    try:
        username, password = os.environ["GOTO_SIP_USERNAME"], os.environ["GOTO_SIP_PASSWORD"]
    except KeyError as missing:
        sys.exit(f"set {missing.args[0]} in the environment")

    with open(args.wav, "rb") as f:
        wav_bytes = f.read()
    voice = GoToVoice(username, password, server=args.server, proxy=args.proxy, transport=args.transport)
    print(await voice.call_and_play(args.to, wav_bytes))


if __name__ == "__main__":
    asyncio.run(main())
