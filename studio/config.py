import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    admin_id: int
    artist_name: str = "Tibris"
    artist_username: str = ""
    bot_name: str = "Tibris × Digital Cube"
    db_path: str = "data/bot_data.db"
    portfolio_url: str = ""
    cube_channel: str = "@Cube_Quest"
    crypto_pay_token: str = ""
    crypto_pay_testnet: bool = False
    yoomoney_wallet: str = ""
    yoomoney_token: str = ""
    yoomoney_card_fee: str = "0.03"

    @classmethod
    def from_env(cls):
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        admin = os.getenv("ADMIN_ID", "").strip()
        if not token or not admin.isdigit() or int(admin) <= 0:
            raise ValueError("Заполните BOT_TOKEN и положительный ADMIN_ID в .env")
        db = os.getenv("DB_PATH", "data/bot_data.db")
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        return cls(
            token,
            int(admin),
            os.getenv("ARTIST_NAME", "Tibris"),
            os.getenv("ARTIST_USERNAME", "").lstrip("@"),
            os.getenv("BOT_NAME", "Tibris × Digital Cube"),
            db,
            os.getenv("PORTFOLIO_URL", ""),
            os.getenv("CUBE_CHANNEL", "@Cube_Quest"),
            os.getenv("CRYPTO_PAY_TOKEN", ""),
            os.getenv("CRYPTO_PAY_TESTNET", "0") == "1",
            os.getenv("YOOMONEY_WALLET", ""),
            os.getenv("YOOMONEY_TOKEN", ""),
            os.getenv("YOOMONEY_CARD_FEE", "0.03"),
        )
