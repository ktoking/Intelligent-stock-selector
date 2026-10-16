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
    option_funding_mode: str = "shared"
    option_equity_fraction: float = 0
    capital_mode: str = "fixed"
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
        for name in ("stock_budget", "max_drawdown_pct", "daily_loss_limit_pct"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"Invalid {name}")
        if self.capital_mode not in ('fixed','broker_equity'):
            raise ValueError('Invalid capital mode')
        if self.option_funding_mode not in ('shared','independent_pending','shared_ratio'):
            raise ValueError('Invalid option funding mode')
        if self.option_funding_mode=='independent_pending':
            if self.option_budget!=0 or self.option_paper_enabled or self.capital_mode!='broker_equity':
                raise ValueError('Unbound independent option account has no executable capital')
        elif self.option_funding_mode=='shared_ratio':
            total=self.stock_budget+self.option_budget
            if (self.capital_mode!='broker_equity' or self.option_equity_fraction!=.1 or
                    not math.isfinite(self.option_budget) or self.option_budget<=0 or
                    not math.isclose(self.option_budget/total,.1,abs_tol=1e-10)):
                raise ValueError('Shared account authorization requires 90/10 capital allocation')
        elif self.option_budget != 1000:
            raise ValueError('Shared option authorization fixes USD 1000; independent account requires binding')
        if self.capital_mode=='fixed' and self.stock_budget!=10000:
            raise ValueError('Fixed authorization is USD 10000/1000; account capital needs explicit migration')
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
        home=Path(home);path=home/'config.json';value=json.loads(path.read_text())
        # Recover an explicitly committed capital migration after a process
        # stopped between SQLite commit and the atomic config replacement.
        if (home/'state.sqlite').exists():
            import sqlite3
            with sqlite3.connect('file:'+str(home/'state.sqlite')+'?mode=ro',uri=True) as db:
                row=db.execute("SELECT value FROM meta WHERE key='capital_settings'").fetchone()
                budget=db.execute("SELECT value FROM meta WHERE key='budget_hash'").fetchone()
            if row and budget and budget[0]!=digest([value['stock_budget'],value['option_budget']]):
                committed=json.loads(row[0])
                if digest([committed['stock_budget'],committed['option_budget']])!=budget[0]:
                    raise ValueError('Committed capital settings inconsistent')
                value={**value,**{k:committed[k] for k in ('stock_budget','option_budget','capital_mode','option_funding_mode','option_equity_fraction','option_paper_enabled') if k in committed}}
                temporary=home/'config.json.tmp';temporary.write_text(canonical(value));temporary.chmod(0o600);temporary.replace(path)
        return cls(**value)


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
