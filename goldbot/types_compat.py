"""Re-export of shared enums, so ``goldbot.risk`` does not import the backtest
package (the live runner needs sizing without pulling in the simulator)."""
from .backtest.types import ExitReason, Position, Side, Signal, Trade  # noqa: F401
