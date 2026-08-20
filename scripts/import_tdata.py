import argparse
import asyncio
import base64
import shutil
import tempfile
import zipfile
from pathlib import Path

from opentele2.td import TDesktop
from opentele2.api import UseCurrentSession


async def convert(source: Path, output: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        if source.is_dir():
            tdata_path = source
        else:
            with zipfile.ZipFile(source, "r") as archive:
                archive.extractall(tmp_path)
            candidates = [tmp_path / "tdata"] + list(tmp_path.rglob("tdata"))
            tdata_path = next((p for p in candidates if p.is_dir()), None)
            if not tdata_path:
                raise RuntimeError("tdata folder not found in archive")

        if not (tdata_path / "key_datas").exists():
            raise RuntimeError("Selected folder does not look like Telegram Desktop tdata")

        tdesk = TDesktop(str(tdata_path))
        if not tdesk.isLoaded():
            raise RuntimeError("Telegram Desktop session could not be loaded. It may require a local passcode or be incomplete.")

        session_tmp = tmp_path / "telegram_reader.session"
        client = await tdesk.ToTelethon(session=str(session_tmp), flag=UseCurrentSession)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Converted session is not authorized")
        me = await client.get_me()
        await client.disconnect()

        shutil.copy2(session_tmp, output)
        encoded = base64.b64encode(output.read_bytes()).decode("ascii")
        print(f"Authorized Telegram account id: {me.id}")
        print(f"Session written to: {output}")
        print("\nTELEGRAM_SESSION_FILE_B64 (store only in Railway Variables):\n")
        print(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Telegram Desktop tdata folder or tdata.zip to a Railway-safe Telethon session secret")
    parser.add_argument("source", type=Path, help="Path to Telegram Desktop tdata folder or tdata.zip")
    parser.add_argument("--output", type=Path, default=Path("telegram_reader.session"))
    args = parser.parse_args()
    asyncio.run(convert(args.source, args.output))


if __name__ == "__main__":
    main()
