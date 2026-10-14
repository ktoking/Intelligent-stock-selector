"""Pin ranking membership while allowing newer bars for the same names."""
from research.daily_factor_lab.core import Panel


def frozen_view(panel,record):
    names=record["evidence"].get("symbols",panel.symbols)
    if set(names)-set(panel.symbols):
        raise ValueError("Frozen universe member missing from refreshed dataset")
    indices=[panel.symbols.index(s) for s in names]
    return Panel(panel.dates,names,panel.stock_mask[indices],
                 {k:v[:,indices] for k,v in panel.fields.items()},panel.metadata)
