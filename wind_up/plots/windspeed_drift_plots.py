import matplotlib.pyplot as plt
import pandas as pd

from wind_up.models import PlotConfig


def plot_rolling_windspeed_diff_one_wtg(
    *,
    wtg_df: pd.DataFrame,
    wtg_name: str,
    ws_col: str,
    plot_cfg: PlotConfig,
) -> None:
    plt.figure()
    plt.plot(wtg_df["rolling_windspeed_diff"])
    plot_title = f"{wtg_name} rolling {ws_col} diff to reanalysis"
    plt.title(plot_title)
    plt.xlabel("datetime")
    plt.ylabel("rolling_windspeed_diff [m/s]")
    plt.grid()
    plt.tight_layout()
    if plot_cfg.show_plots:
        plt.show()
    if plot_cfg.save_plots:
        (plot_cfg.plots_dir / wtg_name).mkdir(exist_ok=True, parents=True)
        plt.savefig(plot_cfg.plots_dir / wtg_name / f"{plot_title}.png")
    plt.close()
