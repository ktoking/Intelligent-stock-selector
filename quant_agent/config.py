from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path.home() / ".local/share/stock-agent/quant-agent"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    stock_budget: float = 10000
    option_budget: float = 1000
    stock_account: str = ""
    stock_market: int = 100
    paper_enabled: bool = False
    parallel_paper: bool = False
    option_paper_enabled: bool = False
    option_max_spread_bps: int = 300
    ai_provider: str = "codex"
    ai_model: str = "gpt-6.1-sol"
    ai_reasoning_effort: str = "high"
    broad_scan_seconds: int = 300
    news_scan_seconds: int = 1800
    quote_push_enabled: bool = True
    evolution_enabled: bool = True
    data_dir: str = str(ROOT / "outputs/watchlist_daily_trend_20260923")
    max_quote_age_seconds: int = 90
    max_spread_bps: int = 50
    fee_reserve_bps: int = 30
    min_shadow_sessions: int = 20
    max_drawdown_pct: float = 15
    daily_loss_limit_pct: float = 2
    poll_seconds: int = 60
    auto_refresh: bool = False
    daily_research: bool = False
    max_trials: int = 30

    def __post_init__(self):
        for name in ("stock_budget", "option_budget", "max_drawdown_pct", "daily_loss_limit_pct"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"Invalid {name}")
        if self.stock_budget != 10000 or self.option_budget != 1000:
            raise ValueError("This authorization fixes stock/options budgets at USD 10000/1000")
        if self.stock_market != 100 or self.ai_provider not in ("ollama", "codex", "configured"):
            raise ValueError("Unverified account market or AI provider")
        if type(self.parallel_paper) is not bool:
            raise ValueError('Invalid parallel paper flag')
        if type(self.option_paper_enabled) is not bool or not 50<=self.option_max_spread_bps<=500:
            raise ValueError('Invalid option paper settings')
        if self.option_paper_enabled and not (self.parallel_paper and self.paper_enabled):
            raise ValueError('Options require authorized parallel simulated execution')
        if self.ai_reasoning_effort not in ('low','medium','high','xhigh','max','ultra') or not self.ai_model:
            raise ValueError('Invalid Codex model settings')
        if not 60<=self.broad_scan_seconds<=3600 or not 300<=self.news_scan_seconds<=86400:
            raise ValueError('Invalid scanner intervals')
        if not (20 <= self.min_shadow_sessions <= 252 and 15 <= self.poll_seconds <= 3600
                and 1 <= self.max_trials <= 100 and 1 <= self.max_quote_age_seconds <= 120
                and 1 <= self.max_spread_bps <= 100 and 10 <= self.fee_reserve_bps <= 100):
            raise ValueError("Invalid gate or execution parameters")

    @property
    def sha256(self):
        return digest(asdict(self))

    @classmethod
    def load(cls, home=DEFAULT_HOME):
        return cls(**json.loads((Path(home) / "config.json").read_text()))


def initialize(home=DEFAULT_HOME, **overrides):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    path = home / "config.json"
    if path.exists():
        return Settings.load(home)
    settings = Settings(**overrides)
    path.write_text(canonical(asdict(settings)))
    path.chmod(0o600)
    return settings
