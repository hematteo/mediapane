"""matplotlib backend: `plt.show()` sends figures to the pane instead of opening windows.

MPLBACKEND=mediapane python train.py                 # matplotlib >= 3.9
MPLBACKEND=module://mediapane.mpl python train.py    # any version
"""

from __future__ import annotations

from matplotlib._pylab_helpers import Gcf
from matplotlib.backend_bases import FigureManagerBase, _Backend
from matplotlib.backends.backend_agg import FigureCanvasAgg

import mediapane


class FigureManagerMediapane(FigureManagerBase):
    def show(self) -> None:
        fig = self.canvas.figure
        label = fig.get_label() or f"figure {self.num}"
        mediapane.show(fig, name=f"{label}.png")


class FigureCanvasMediapane(FigureCanvasAgg):
    # matplotlib >= 3.6 builds managers from the canvas's manager_class
    manager_class = FigureManagerMediapane


@_Backend.export
class _BackendMediapane(_Backend):
    FigureCanvas = FigureCanvasMediapane
    FigureManager = FigureManagerMediapane

    @classmethod
    def show(cls, *, block: bool | None = None) -> None:
        # Like notebook inline backends: send every open figure once, then close them, so the
        # next plt.show() doesn't resend old plots
        for manager in Gcf.get_all_fig_managers():
            manager.show()
        Gcf.destroy_all()
