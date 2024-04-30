import logging
import zipfile
from functools import partial
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal
from scipy.stats import circmean
from tabulate import tabulate

from examples.helpers import download_zenodo_data
from wind_up.caching import with_parquet_cache
from wind_up.constants import OUTPUT_DIR, PROJECTROOT_DIR, TIMESTAMP_COL, DataColumns
from wind_up.interface import AssessmentInputs
from wind_up.main_analysis import run_wind_up_analysis
from wind_up.models import PlotConfig, WindUpConfig
from wind_up.reanalysis_data import ReanalysisDataset

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
CACHE_FLD = PROJECTROOT_DIR / "cache" / "smarteole_example_data"

ENSURE_DOWNLOAD = 1
CHECK_RESULTS = 1
PARENT_DIR = Path(__file__).parent
ZIP_FILENAME = "SMARTEOLE-WFC-open-dataset.zip"
MINIMUM_DATA_COUNT_COVERAGE = 0.5  # 50% of the data must be present


@with_parquet_cache(CACHE_FLD / "_smarteole_scada.parquet")
def _unpack_scada() -> pd.DataFrame:
    """
    Function that translates 1-minute SCADA data to 10 minute data in the wind-up expected format
    """

    def _separate_turbine_id_from_field(x: str) -> tuple[str, str]:
        parts = x.split("_")
        if len(parts[-1]) == 1:
            wtg_id = parts[-1]
            col_name = "_".join(parts[:-1])
        else:
            wtg_id = parts[-2]
            col_name = "_".join(parts[:-2] + [parts[-1]])
        return f"SMV{wtg_id}", col_name

    def _make_turbine_id_a_column(df: pd.DataFrame) -> pd.DataFrame:
        df.columns = pd.MultiIndex.from_tuples(
            (_separate_turbine_id_from_field(i) for i in df.columns),
            names=[DataColumns.turbine_name, "field"],
        )
        return df.stack(level=0, future_stack=True).reset_index(DataColumns.turbine_name)  # noqa: PD013

    def _map_and_mask_cols(df: pd.DataFrame) -> pd.DataFrame:
        ten_minutes_count_lower_limit = 600 * MINIMUM_DATA_COUNT_COVERAGE
        mask_active_power = df["active_power_count"] < ten_minutes_count_lower_limit
        mask_wind_speed = df["wind_speed_count"] < ten_minutes_count_lower_limit
        mask_pitch_angle = df["blade_1_pitch_angle_count"] < ten_minutes_count_lower_limit
        mask_gen_rpm = df["generator_speed_count"] < ten_minutes_count_lower_limit
        mask_temperature = df["temperature_count"] < ten_minutes_count_lower_limit
        mask_nacelle_position = df["nacelle_position_count"] < ten_minutes_count_lower_limit
        return df.assign(
            **{
                DataColumns.active_power_mean: lambda d: d["active_power_avg"].mask(mask_active_power),
                DataColumns.active_power_sd: lambda d: d["active_power_std"].mask(mask_active_power),
                DataColumns.wind_speed_mean: lambda d: d["wind_speed_avg"].mask(mask_wind_speed),
                DataColumns.wind_speed_sd: lambda d: d["wind_speed_std"].mask(mask_wind_speed),
                DataColumns.yaw_angle_mean: lambda d: d["nacelle_position_avg"].mask(mask_nacelle_position),
                DataColumns.yaw_angle_min: lambda d: d["nacelle_position_min"].mask(mask_nacelle_position),
                DataColumns.yaw_angle_max: lambda d: d["nacelle_position_max"].mask(mask_nacelle_position),
                DataColumns.pitch_angle_mean: lambda d: d["blade_1_pitch_angle_avg"].mask(mask_pitch_angle),
                DataColumns.gen_rpm_mean: lambda d: d["generator_speed_avg"].mask(mask_gen_rpm),
                DataColumns.ambient_temp: lambda d: d["temperature_avg"].mask(mask_temperature),
                DataColumns.shutdown_duration: 0,
            }
        )

    # unzipping the data in memory and only reading the relevant files
    scada_fpath = "SMARTEOLE-WFC-open-dataset/SMARTEOLE_WakeSteering_SCADA_1minData.csv"
    circular_mean = partial(circmean, low=0, high=360)
    with zipfile.ZipFile(CACHE_FLD / ZIP_FILENAME) as zf:
        return (
            pd.read_csv(zf.open(scada_fpath), parse_dates=[0], index_col=0)
            .pipe(_make_turbine_id_a_column)
            .groupby(DataColumns.turbine_name)
            .resample("10min")
            .aggregate(
                {
                    "active_power_avg": "mean",
                    "active_power_std": "mean",
                    "active_power_count": "sum",
                    "wind_speed_avg": "mean",
                    "wind_speed_std": "mean",
                    "wind_speed_count": "sum",
                    "blade_1_pitch_angle_avg": "mean",  # no need for circular_mean for small angles
                    "blade_1_pitch_angle_count": "sum",
                    "generator_speed_avg": "mean",
                    "generator_speed_count": "sum",
                    "temperature_avg": "mean",
                    "temperature_count": "sum",
                    "nacelle_position_avg": circular_mean,
                    "nacelle_position_max": "max",
                    "nacelle_position_min": "min",
                    "nacelle_position_count": "sum",
                }
            )
            .reset_index(DataColumns.turbine_name)
            .pipe(_map_and_mask_cols)
            .loc[:, DataColumns.all()]
            .rename_axis(TIMESTAMP_COL, axis=0)
            .rename_axis(None, axis=1)
        )


@with_parquet_cache(CACHE_FLD / "_smarteole_metadata.parquet")
def _unpack_metadata() -> pd.DataFrame:
    md_fpath = "SMARTEOLE-WFC-open-dataset/SMARTEOLE_WakeSteering_Coordinates_staticData.csv"
    with zipfile.ZipFile(CACHE_FLD / ZIP_FILENAME) as zf:
        return (
            pd.read_csv(zf.open(md_fpath), index_col=0)
            .reset_index()
            .rename(columns={"Turbine": "Name"})
            .query("Name.str.startswith('SMV')")  # is a turbine
            .loc[:, ["Name", "Latitude", "Longitude"]]
            .assign(TimeZone="UTC", TimeSpanMinutes=10, TimeFormat="Start")
        )


@with_parquet_cache(CACHE_FLD / "_smarteole_toggle.parquet")
def _unpack_toggle_data() -> pd.DataFrame:
    ten_minutes_count_lower_limit = 600 * MINIMUM_DATA_COUNT_COVERAGE
    toggle_value_threshold: float = 0.95

    _fpath = "SMARTEOLE-WFC-open-dataset/SMARTEOLE_WakeSteering_ControlLog_1minData.csv"
    with zipfile.ZipFile(CACHE_FLD / ZIP_FILENAME) as zf:
        raw_df = pd.read_csv(zf.open(_fpath), parse_dates=[0], index_col=0)

    required_in_cols = [
        "control_log_offset_active_avg",
        "control_log_offset_active_count",
    ]
    toggle_df = (
        raw_df[required_in_cols]
        .resample("10min")
        .agg({"control_log_offset_active_avg": "mean", "control_log_offset_active_count": "sum"})
    )
    toggle_df["toggle_on"] = (toggle_df["control_log_offset_active_avg"] >= toggle_value_threshold) & (
        toggle_df["control_log_offset_active_count"] >= ten_minutes_count_lower_limit
    )
    toggle_df["toggle_off"] = (toggle_df["control_log_offset_active_avg"] <= (1 - toggle_value_threshold)) & (
        toggle_df["control_log_offset_active_count"] >= ten_minutes_count_lower_limit
    )

    # timestamps represent UTC start format according to Thomas Duc
    toggle_df.index = toggle_df.index.tz_localize("UTC")
    toggle_df.index.name = TIMESTAMP_COL
    return toggle_df[["toggle_on", "toggle_off"]]


if __name__ == "__main__":
    logger.info("Downloading example data from Zenodo")
    download_zenodo_data(record_id="7342466", output_dir=CACHE_FLD, filenames={ZIP_FILENAME})

    logger.info("Preprocessing (and caching) turbine SCADA data")
    scada_df = _unpack_scada()
    logger.info("Preprocessing (and caching) turbine metadata")
    metadata_df = _unpack_metadata()
    logger.info("Preprocessing (and caching) toggle data")
    toggle_df = _unpack_toggle_data()

    logger.info("Loading reference reanalysis data")
    reanalysis_dataset = ReanalysisDataset(
        id="ERA5T_50.00N_2.75E_100m_1hr",
        data=pd.read_parquet(PARENT_DIR / "ERA5T_50.00N_2.75E_100m_1hr_20200201_20200531.parquet"),
    )

    logger.info("Defining Assessment Configuration")
    wtg_map = {
        f"SMV{i}": {
            "name": f"SMV{i}",
            "turbine_type": {
                "turbine_type": "Senvion-MM82-2050",
                "rotor_diameter_m": 82.0,
                "rated_power_kw": 2050.0,
                "cutout_ws_mps": 25,
                "normal_operation_pitch_range": (-10.0, 35.0),
                "normal_operation_genrpm_range": (250.0, 2000.0),
                "rpm_v_pw_margin_factor": 0.05,
                "pitch_to_stall": False,
            },
        }
        for i in range(1, 7 + 1)
    }
    northing_corrections_utc = [
        ("SMV1", pd.Timestamp("2020-02-17 16:30:00+0000"), 5.750994540354649),
        ("SMV2", pd.Timestamp("2020-02-17 16:30:00+0000"), 5.690999999999994),
        ("SMV3", pd.Timestamp("2020-02-17 16:30:00+0000"), 5.558000000000042),
        ("SMV4", pd.Timestamp("2020-02-17 16:30:00+0000"), 5.936999999999996),
        ("SMV5", pd.Timestamp("2020-02-17 16:30:00+0000"), 6.797253350869262),
        ("SMV6", pd.Timestamp("2020-02-17 16:30:00+0000"), 5.030130916842758),
        ("SMV7", pd.Timestamp("2020-02-17 16:30:00+0000"), 4.605999999999972),
    ]

    cfg = WindUpConfig(
        assessment_name="smarteole_example",
        require_ref_wake_free=True,
        detrend_min_hours=12,
        ref_wd_filter=[197.0, 246.0],
        filter_all_test_wtgs_together=True,
        use_lt_distribution=False,
        out_dir=OUTPUT_DIR / "smarteole_example",
        test_wtgs=[wtg_map["SMV6"], wtg_map["SMV5"]],
        ref_wtgs=[wtg_map["SMV7"]],
        ref_super_wtgs=[],
        non_wtg_ref_names=[],
        analysis_first_dt_utc_start=pd.Timestamp("2020-02-17 16:30:00+0000"),
        upgrade_first_dt_utc_start=pd.Timestamp("2020-02-17 16:30:00+0000"),
        analysis_last_dt_utc_start=pd.Timestamp("2020-05-24 23:50:00+0000"),
        lt_first_dt_utc_start=pd.Timestamp("2020-02-17 16:30:00+0000"),
        lt_last_dt_utc_start=pd.Timestamp("2020-05-24 23:50:00+0000"),
        detrend_first_dt_utc_start=pd.Timestamp("2020-02-17 16:30:00+0000"),
        detrend_last_dt_utc_start=pd.Timestamp("2020-05-24 23:50:00+0000"),
        years_for_lt_distribution=0,
        years_for_detrend=0,
        ws_bin_width=1.0,
        asset={
            "name": "Sole du Moulin Vieux",
            "wtgs": list(wtg_map.values()),
            "masts_and_lidars": [],
        },
        northing_corrections_utc=northing_corrections_utc,
        toggle={
            "toggle_file_per_turbine": False,
            "toggle_filename": "SMV_offset_active_toggle_df.parquet",
            "detrend_data_selection": "use_toggle_off_data",
            "pairing_filter_method": "any_within_timedelta",
            "pairing_filter_timedelta_seconds": 3600,
            "toggle_change_settling_filter_seconds": 600,
        },
    )
    plot_cfg = PlotConfig(show_plots=False, save_plots=True, plots_dir=cfg.out_dir / "plots")

    assessment_inputs = AssessmentInputs.from_cfg(
        cfg=cfg,
        plot_cfg=plot_cfg,
        toggle_df=toggle_df,
        scada_df=scada_df,
        metadata_df=metadata_df,
        reanalysis_datasets=[reanalysis_dataset],
        cache_dir=CACHE_FLD,
    )
    results_per_test_ref_df = run_wind_up_analysis(assessment_inputs)

    if CHECK_RESULTS:
        # print key results to console
        key_results_df = results_per_test_ref_df[
            [
                "test_wtg",
                "ref",
                "uplift_frc",
                "unc_one_sigma_frc",
                "uplift_p95_frc",
                "uplift_p5_frc",
                "pp_valid_hours_pre",
                "pp_valid_hours_post",
                "mean_power_post",
            ]
        ]

        def convert_frc_cols_to_pct(input_df: pd.DataFrame, dp: int = 1) -> pd.DataFrame:
            for i, col in enumerate(x for x in input_df.columns if x.endswith("_frc")):
                if i == 0:
                    output_df = input_df.assign(**{col: (input_df[col] * 100).round(dp).astype(str) + "%"})
                else:
                    output_df = output_df.assign(**{col: (input_df[col] * 100).round(dp).astype(str) + "%"})
                output_df = output_df.rename(columns={col: col.replace("_frc", "_pct")})
            return output_df

        print_df = convert_frc_cols_to_pct(key_results_df).rename(
            columns={
                "test_wtg": "turbine",
                "ref": "reference",
                "uplift_pct": "energy uplift",
                "unc_one_sigma_pct": "uplift uncertainty",
                "uplift_p95_pct": "uplift P95",
                "uplift_p5_pct": "uplift P5",
                "pp_valid_hours_pre": "valid hours toggle off",
                "pp_valid_hours_post": "valid hours toggle on",
                "mean_power_post": "mean power toggle on",
            }
        )
        print_df["mean power toggle on"] = print_df["mean power toggle on"].round(0).astype("int64")
        results_table = tabulate(
            print_df,
            headers="keys",
            tablefmt="fancy_grid",
            floatfmt=".1f",
            numalign="center",
            stralign="center",
            showindex=False,
        )
        print(results_table)

        # raise an error if results don't match expected
        expected_print_df = pd.DataFrame(
            {
                "turbine": ["SMV6", "SMV5"],
                "reference": ["SMV7", "SMV7"],
                "energy uplift": ["-1.1%", "3.0%"],
                "uplift uncertainty": ["0.6%", "1.2%"],
                "uplift P95": ["-2.1%", "1.1%"],
                "uplift P5": ["-0.2%", "5.0%"],
                "valid hours toggle off": [137 + 5 / 6, 137 + 4 / 6],
                "valid hours toggle on": [136.0, 137 + 1 / 6],
                "mean power toggle on": [1148, 994],
            },
            index=[0, 1],
        )

        assert_frame_equal(print_df, expected_print_df)