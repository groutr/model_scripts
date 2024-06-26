"""
Waterlevel and tidal constituent solver/plotter.

Use this script to plot water level and optionally solve and plot tidal constitents.

Example Usages:
Plot waterlevel only:
python wl_plotter.py --output out_plots <dflow_history_path> <obs_path>

To solve for tidal constituents and plot results
python wl_plotter.py -t --output out_plots <dflow_history_path> <obs_path>

External Dependencies:
  xarray
  numpy
  pandas
  scipy
  matplotlib
  pytides (from https://github.com/groutr/pytides)
"""

import xarray as xr
import pandas as pd
import numpy as np
import cftime
import time
import math
import json
import argparse
import datetime
import pathlib
from dataclasses import dataclass
from enum import Enum, auto
from scipy.stats import pearsonr

try:
    from pytides.tide import Tide
    from pytides.astro import astro
    from pytides.constituent import noaa as noaa_constituents
    have_pytides = True
except ImportError:
    have_pytides = False

import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# Turn off SettingWithCopyWarning (we are careful not to do that)
pd.options.mode.chained_assignment = None

# Minimum number of hours in timeseries needed to differentiate 
# tidal constituents from each other.
MINHOURS = {
	'M2': 12,
	'M4': 12,
	'M6': 12,
	'M8': 12,
	'K1': 27,
	'S6': 118,
	'2MK3': 329,
	'MK3': 329,
	'O1': 329,
	'OO1':  329,
	'2Q1': 332,
	'2SM2': 356,
	'MS4': 356,
	'S2': 356,
	'S4': 356,
	'M1': 656,
	'M3': 656,
	'J1': 663,
	'MN4': 663,
	'N2': 663,
	'Q1': 663,
	'L2': 764,
	'MM': 764,
	'MU2': 764,
	'K2': 4383,
	'MF': 4383,
	'MSF': 4383,
	'P1': 4383,
	'2N2': 4942,
	'LAMBDA2': 4942,
	'NU2': 4942,
	'RHO1': 4942
}

# Set of constituents to plot
PLOT_CONSTITUENTS = ['M2', 'S2', 'N2', 'K2', 'O1', 'K1', 'Q1', 'P1']

class Datum(Enum):
    Unknown = auto()
    MSL = auto()
    NAVD88 = auto()

@dataclass
class TSData:
    """Store data for a station

    Dataframe is structured as observed/predicted data in columns.
    Several methods are provided to compute statistics between
    the observed data and predicted data.
    """
    datum: Datum
    station_id: str
    data: pd.DataFrame
    bias_correct: bool = False

    def __init__(self, datum, station_id, data, bias_correct=False):
        self.datum = datum
        self.station_id = station_id
        self.data = data.copy()
        self.bias_correct = bias_correct

        if bias_correct:
            self.data.observation += (data.model - data.observation).mean()

    def __len__(self):
        return len(self.data)

    def __hash__(self) -> int:
        return int(self.station_id)

    @property
    def predicted(self):
        return self.data.model

    @property
    def observed(self):
        return self.data.observation

    def bias(self):
        return (self.predicted - self.observed).mean()

    def rmse(self):
        return math.sqrt(np.square(self.predicted - self.observed).mean())

    def skill(self):
        n = (np.square(self.predicted - self.observed)).sum()
        om = self.observed.mean()
        d = (self.predicted - om).abs() + (self.observed - om).abs()
        d = np.square(d).sum()
        return 1 - n/d
    
    def r(self):
        r1 = (self.predicted - self.predicted.mean())/self.predicted.std()
        r2 = (self.observed - self.observed.mean())/self.observed.std()
        return 1/(len(self)-1) * np.sum(r1 * r2)

    def corr(self):
        return pearsonr(self.predicted, self.observed)

    def range(self):
        return self.data.max() - self.data.min()

    def nrmse(self):
        return 100 * self.rmse()/self.range().observation


def mae_phase(model, obs):
    tmp = np.abs(phase_correct(obs - model))
    return np.sum(tmp)/len(tmp)


def mre(model, obs):
    tmp = np.abs(obs - model)/obs
    return np.sum(tmp)/len(tmp)


def mean_rmse(Am, An, Pm, Pn):
    tmp = 0.5 * (Am ** 2 + An ** 2) - Am * An * np.cos(np.pi*phase_correct(Pm - Pn)/180)
    return np.sum(np.sqrt(tmp))/len(tmp)


def rmse(model, obs):
    return math.sqrt(np.square(model - obs).mean())


def phase_correct(pdiff):
    """Correct phase difference so all readings are in [0, 360]"""
    pdiff[pdiff > 180] = 360 - pdiff[pdiff > 180]
    pdiff[pdiff < -180] = 360 + pdiff[pdiff < -180]
    return pdiff


def tidal_analysis(d):
    """Solve for tidal constituents.

    Args:
        d (TSData): [description]

    Returns:
        tuple: (DataFrame, DataFrame)
    """
    # Filter list of constituents by MIN_HOURS
    # to only solve for constituents for which we have enough data.
    newdata = d.data
    t = newdata.index.to_pydatetime()
    thrs = (t[-1] - t[0]).total_seconds() / 3600
    _constituents = []
    for c in noaa_constituents:
        minh = MINHOURS.get(str(c))
        if minh and thrs >= minh:
            # If we have enough data (higher than minh)
            _constituents.append(c)
        elif minh is None and thrs >= 24*366:
            # If we have 1 year of data, add constituent
            _constituents.append(c)
    _cstrs = list(map(str, _constituents))

    # Solve for predicted (model) constituents
    start = time.perf_counter()
    tide = Tide.decompose(d.predicted.values, t, constituents=_constituents)
    print(d.station_id, "Model solve: ", time.perf_counter() - start, "for", _cstrs)
    data = ((x['constituent'].name, 
            x['constituent'].speed(astro(t[0])), 
            x['amplitude'], 
            x['phase']) for x in tide.model)
    model_rv = pd.DataFrame(list(data), columns=['constituent', 'speed', 'amplitude', 'phase'])
    model_rv = model_rv.set_index('constituent')

    # Solve for constituents in observed values
    water_lev = d.observed
    t = water_lev.index.to_pydatetime()
    start = time.perf_counter()
    tide = Tide.decompose(water_lev.values, t, constituents=_constituents)
    print(d.station_id, "observed solve: ", time.perf_counter() - start, "for", _cstrs)

    data = ((x['constituent'].name, 
            x['constituent'].speed(astro(t[0])),
            x['amplitude'], 
            x['phase']) for x in tide.model)
    
    obs_rv = pd.DataFrame(list(data), columns=['constituent', 'speed', 'amplitude', 'phase'])
    obs_rv = obs_rv.set_index('constituent')
    return model_rv, obs_rv

def tide_plots(model_tide, obs_tide, out_path, station_id):
    # Write tidal constitent csv reports
    model_tide.to_csv(out_path/f"{station_id}_model.csv")
    obs_tide.to_csv(out_path/f"{station_id}_obs.csv")

    # Select only the plotting constituents if they exist to prep for plotting
    mt = model_tide.loc[model_tide.index.intersection(PLOT_CONSTITUENTS)]
    ot = obs_tide.loc[obs_tide.index.intersection(PLOT_CONSTITUENTS)]
    if mt.empty or ot.empty:
        return
    
    mt['phase_hr'] = mt['phase']/mt['speed']
    ot['phase_hr'] = ot['phase']/ot['speed']
    fig, axs = plt.subplots(1, 3, figsize=(20, 10))
    fig.tight_layout()
    fig.suptitle(f"NOAA Station {station_id}", size=20, fontweight="bold")
    
    # Amplitude plot
    ax = axs[0]
    ax.set_aspect('equal')
    m, o = mt['amplitude'].align(ot['amplitude'])
    axlim = maxlim(m, o)
    ax.set_xlim([0, axlim])
    ax.set_ylim([0, axlim])
    ax.set_xlabel("Amplitude (m) - NOAA observation")
    ax.set_ylabel("Amplitude (m) - Model prediction")
    ax.plot([0, axlim], [0, axlim], 'k--', alpha=.5)
    ax.scatter(o.values, m.values)
    for x, y, label in zip(o, m, m.index):
        ax.text(x, y, label, size=15)

    # Phase plot
    ax = axs[1]
    ax.set_aspect('equal')
    m, o = mt['phase'].align(ot['phase'])
    axlim = maxlim(m, o)
    ax.set_xlim([0, axlim])
    ax.set_ylim([0, axlim])
    ax.set_xlabel('Phase (deg.) - NOAA prediction')
    ax.set_ylabel('Phase (deg.) - Model results')
    ax.plot([0, axlim], [0, axlim], 'k--', alpha=.5)
    ax.scatter(o.values, m.values)
    for x, y, label in zip(o, m, m.index):
        ax.text(x, y, label, size=15)

    # Phase hour plot
    ax = axs[2]
    ax.set_aspect('equal')
    m, o = mt['phase_hr'].align(ot['phase_hr'])
    axlim = maxlim(m, o)
    ax.set_xlim([0, axlim])
    ax.set_ylim([0, axlim])
    ax.set_xlabel('Phase (hr.) - NOAA prediction')
    ax.set_ylabel('Phase (hr.) - Model results')
    ax.plot([0, axlim], [0, axlim], 'k--', alpha=.5)
    ax.scatter(o.values, m.values)
    for x, y, label in zip(o, m, m.index):
        ax.text(x, y, label, size=15)
    plt.savefig(out_path/f"{station_id}_tidal.png", dpi=200, bbox_inches="tight")
    plt.close(fig)


def tidal_error(model, obs, out_path):
    """Compute and report error statistics

    Args:
        model (DataFrame): Tidal constitents for model
        obs (DataFrame): Tidal constitents for observations
        out_path (pathlib.Path): Output path
    """
    model = model[model.constituent.isin(PLOT_CONSTITUENTS)]
    obs = obs[obs.constituent.isin(PLOT_CONSTITUENTS)]
    M = model.pivot(index='station', columns='constituent', values=['phase', 'amplitude'])
    O = obs.pivot(index='station', columns='constituent', values=['phase', 'amplitude'])

    M.to_csv(out_path/"Model_solved_tidal.csv")
    O.to_csv(out_path/"NOAA_solved_tidal.csv")


    _const = M.columns.get_level_values('constituent').unique()
    stats = pd.DataFrame(index=_const, columns=['MAE', 'MRE', 'MRMSE'])

    for c in _const:
        stats.loc[c, "MAE"] = mae_phase(M[("phase", c)], O[("phase", c)])

    for c in _const:
        stats.loc[c, "MRE"] = mre(M[("amplitude", c)], O[("amplitude", c)])

    for c in _const:
        x = ("amplitude", c)
        y = ("phase", c)
        stats.loc[c, "MRMSE"] = mean_rmse(M[x], O[x], M[y], O[y])

    stats.to_csv(out_path/"Mean_stats.csv")


def amplitude_plot(model, obs, out_path):
    ref_lines = {'linewidth': 1, 'alpha': .3}
    data_opts = {'markeredgecolor': 'k', 
                'color': 'b', 
                'marker': 'o', 
                'markersize': 5,
                'linestyle': ''}
    fig, axs = plt.subplots(1, 2, figsize=(12, 6))
    for c in PLOT_CONSTITUENTS:
        M = model[model.constituent == c]
        O = obs[obs.constituent == c]
        if M.empty or O.empty:
            continue
        
        print("Plotting", c, "phase amplitude")
        
        fig.suptitle(f"{c} constituent", size=20, fontweight='bold')

        ax = axs[0]
        maxav = 1.2 * max(M.amplitude.max(), O.amplitude.max())
        axlim = max(round(maxav, 1), 0.05)
        ax.set_xlim([0, axlim])
        ax.set_ylim([0, axlim])
        ax.set_xlabel("Amplitude (m) - NOAA", size=12)
        ax.set_ylabel("Amplitude (m) - Model", size=12)
        ax.plot([0, axlim], [0, axlim], 'k', **ref_lines)
        ax.plot([0, axlim], [0, 0.95*axlim], 'k--', **ref_lines)
        ax.plot([0, axlim], [0, 0.9*axlim], 'k:', **ref_lines)
        ax.plot([0, 0.95*axlim], [0, axlim], 'k--', **ref_lines)
        ax.plot([0, 0.9*axlim], [0, axlim], 'k:', **ref_lines)
        ax.plot(O.amplitude, M.amplitude, **data_opts)
        tmp = pearsonr(M.amplitude, O.amplitude)[0]
        ax.annotate(f"Corr: {round(tmp, 3)}", xy=(0.8, 0.03), xycoords='axes fraction', fontsize=8, bbox=dict(boxstyle="square", fc="white", ec="white"))


        ax = axs[1]
        diff = M.phase - O.phase
        M.loc[diff < -180, "phase"] = M.loc[diff < -180, "phase"] + 360
        O.loc[diff > 180, "phase"]  = O.loc[diff > 180, "phase"] + 360
        axlim = 400
        ax.set_xlim([0, axlim])
        ax.set_ylim([0, axlim])
        ax.set_xlabel("Phase (deg) - NOAA", size=12)
        ax.set_ylabel("Phase (deg) - Model", size=12)
        ax.plot([0,axlim],[0,axlim],'k', **ref_lines)
        ax.plot([10,axlim],[0,axlim-10],'k--', **ref_lines)
        ax.plot([20,axlim],[0,axlim-20],'k:', **ref_lines)
        ax.plot([0,axlim-10],[10,axlim],'k--', **ref_lines)
        ax.plot([0,axlim-20],[20,axlim],'k:', **ref_lines)
        ax.plot(O.phase, M.phase, **data_opts)
        tmp = pearsonr(M.phase, O.phase)[0]
        ax.annotate(f"Corr: {round(tmp, 3)}", xy=(0.8, 0.03), xycoords='axes fraction', fontsize=8, bbox=dict(boxstyle="square", fc="white", ec="white"))

        plt.savefig(out_path/f"{c}_AP_Comparison.png", dpi=600, bbox_inches='tight')
        axs[0].cla()
        axs[1].cla()
    plt.close(fig)


def open_nc(filename):
    with xr.open_dataset(filename, decode_cf=True, use_cftime=True) as DS:
        #DS = DS.resample(time='6min').nearest()
        try:
            obs = DS["water_surface_height_above_reference_datum"]
            obs = obs.drop_vars(['altitude', 'latitude', 'longitude'])
            obsdf = obs.to_dataframe()
            obsdf.index = DS.indexes['time'].to_datetimeindex()
            return obsdf.rename(columns={"water_surface_height_above_reference_datum": "observation"})
        except KeyError:
            return pd.DataFrame()
        

def open_csv(filename):
    possible_dates = ["Date (utc)", "Date Time", "Date and Time (GMT)"]
    possible_data = ["gage height (m)", "Water Level", 
                    "Water level (m NAVD88)", 
                    "Elevation ocean/est (m NAVD88)", 
                    "Prediction",
                    "Stream water level elevation above NAVD 1988 (m)"]

    obs_ds = pd.read_csv(filename, low_memory=False)
    # Remove possible spaces in column names
    obs_ds.columns = obs_ds.columns.str.strip()

    # Get date column
    date_col = obs_ds.columns.isin(possible_dates)
    values_col = obs_ds.columns.isin(possible_data)

    if np.count_nonzero(date_col) != 1 and np.count_nonzero(values_col) != 1:
        raise RuntimeError(f"Unknown CSV format: {filename}")
    
    date_label = obs_ds.columns[date_col][0]
    value_label = obs_ds.columns[values_col][0]
    if value_label.lower() != "prediction":
        rname = 'measurement'
    else:
        rname = 'prediction'
    df = obs_ds.loc[:, [date_label, value_label]].rename(columns={date_label: "date", value_label: rname})
    df["date"] = pd.to_datetime(df["date"])
    df = df.loc[pd.notna(df["date"])]
    df = df.set_index("date").sort_index().tz_localize(None)

    return df


def higher_resolution(df1, df2):
    """Return the argument with the higher resolution"""
    def mode(data):
        values, counts = np.unique(np.diff(data), return_counts=True)
        return values[counts.argmax()]

    # Higher resolution timeseries has smaller diff
    if mode(df1.index) < mode(df2.index):
        return df1
    else:
        return df2
    

def maxlim(model, obs):
    # Compute max axis value for plots.
    maxav = 1.2 * max(model.max(), obs.max())
    axlim = max(round(maxav, 1), 0.1)
    return axlim


def open_his_waterlevel(fn):
    with xr.open_mfdataset(fn) as ds:
        ds['station_name'] = ds.station_name.compute().str.strip()
        waterlevel = ds.waterlevel
        waterlevel = waterlevel.set_index(stations='station_name').sortby('stations')
        return waterlevel
    
def open_schism_elevation(fn):
    def convert_schism_time(times):
        _date_parts = times.base_date.split()
        base_date = datetime.datetime(*map(int, _date_parts[:3]))
        if len(_date_parts) >= 4:
            base_date += datetime.timedelta(hours=float(_date_parts[3]))
        rv = cftime.num2date(times.values, f"seconds since {base_date.isoformat()}")
        return rv.astype('datetime64[ns]')

    with xr.open_mfdataset(fn) as ds:
        elevation = ds.elevation
        if elevation['time'].dtype.kind != "M":
            elevation["time"] = convert_schism_time(elevation["time"])
        return elevation

def select_schism_nodes(data, indexer):
    # Indexer will be a string
    indexer = json.loads(indexer)
    _elevation = data[:, indexer]
    _elevation = _elevation.mean(axis=1)

    model = _elevation.squeeze().to_dataframe().rename(columns={'elevation': 'model'})
    return model

def select_dflow_sites(data, indexer):
    index = data.stations.str.strip().values
    waterlevel = data[:, index == indexer]

    model = waterlevel.drop_vars(['station_x_coordinate', 'station_y_coordinate', 'stations'])
    model = model.squeeze().to_dataframe().rename(columns={"waterlevel": "model"})
    return model
    

def read_correspondence_table(path, storms):
    # Correspondence table has the following columns
    # GageID: gages to process
    # Nodes [optional]: SCHSIM nodes to select for GageID
    # ProcessedCSVLoc: Processed csv measurement data
    # Storm: storm identifier
    # Datum: datum identifier
    correspond = pd.read_csv(path, dtype={'GageID': 'string'},
                            converters={'ProcessedCSVLoc': pathlib.Path})
    correspond['GageID'] = correspond['GageID'].str.strip()
    correspond = correspond.set_index('GageID').sort_index()

    if "Storm" in correspond.columns:
        # Filter by storm before uniqueness check
        storm_mask = correspond["Storm"].isin(storms)
        correspond = correspond.loc[storm_mask]
    if not correspond.index.is_unique:
        print(correspond.index[correspond.index.duplicated()])
        raise RuntimeError("GageID needs to be unique")
    if "Datum" in correspond.columns:
        correspond.loc[pd.isna(correspond["Datum"]), "Datum"] = None
    else:
        correspond['Datum'] = None
    return correspond


def main(args):
    cutter = datetime.timedelta(hours=args.cut)
    summary = []
    tidal_summary = []

    correspond = read_correspondence_table(args.correspond, args.storm)

    args.output.mkdir(parents=True, exist_ok=True)

    if args.model_type == "schism":
        waterlevels = open_schism_elevation(args.model)
        mlabel = "SCHISM"
    elif args.model_type == "dflow":
        waterlevels = open_his_waterlevel(args.model)
        mlabel = "DFlow"
    
    for station in correspond.index:
        metadata = correspond.loc[station]
        fn = metadata["ProcessedCSVLoc"]
        if not fn.name:
            continue
        path = args.obs / fn
        if not path.is_file():
            print("Skipping", path, "(data file not found)")
            continue

        # Select station from waterlevel file
        if args.model_type == "schism":
            nodes = metadata['Nodes']
            model = select_schism_nodes(waterlevels, nodes)
        else:
            model = select_dflow_sites(waterlevels, station.encode())

        if np.isnan(model.values).all():
            continue
        
        T = model.index[model.index >= model.index[0]+cutter]
        # drop first twelve hours of model to remove warmup effects
        model = model.loc[T]
        model.index = model.index.tz_localize(None)
        modelfreq = model.index[1] - model.index[0]

        # Get the observation data
        obs = open_csv(path)
        obs_label = obs.columns[0]
        obs = obs.rename(columns={obs_label: 'observation'})

        # Restrict observation range to model range
        obs = obs.loc[model.index[0]:model.index[-1]+modelfreq]
        
        # At times obs is malformed csv, so we check if len == 1.
        if obs.empty or len(obs) == 1 or model.empty:
            continue
        obsfreq = obs.index[1] - obs.index[0]
        
        # Resample model to frequency of obs
        if higher_resolution(obs, model) is obs:
            # Downsample observations to match model, but do not interpolate
            obs_data = obs.resample(modelfreq, origin=model.index[0]).asfreq()
            model_data = model
        else:
            # Resample and interpolate model
            model_data = model.resample(obsfreq, origin=obs.index[0]).interpolate(method='linear')
            obs_data = obs.asfreq(obsfreq)  # Ensure that obs is regular

        # Drop leading/trailing nans from obs
        joined = model_data.join(obs_data, how='inner').sort_index().dropna()
        if joined.empty:
            print("Joined dataframe is empty for station", station)
            continue                
        
        d = TSData(metadata['Datum'], station, joined, bias_correct=args.bias_correct)

        print("writing and plotting", d.station_id)
        d.data.to_csv(args.output/f'{d.station_id}.csv')
        fig = plt.figure(figsize=(10, 5))
        ax = plt.gca()
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))

        ax.plot(d.observed, 'r', marker=',', linestyle='-', label=obs_label.capitalize(), linewidth=2)
        ax.plot(d.predicted, 'b', marker=',', linestyle='-', label=mlabel, linewidth=2)
        ax.legend()
        ax.grid()
        ax.set_title(f"Station ID: {d.station_id}", size=20, fontweight='bold', color='k')

        if d.datum:
            ax.set_ylabel(f"Water level (m {d.datum})", size=15)
        else:
            ax.set_ylabel("Water level (m)", size=15)

        ax.set_xlabel(f"Date [{d.data.index[0].year}]", size=15)

        measures = (d.bias(), d.corr()[0], d.rmse(), d.nrmse(), d.skill())
        # add timeseries statistics
        summary.append((d.station_id,) + measures)
        measures = tuple(round(x, 3) for x in measures)
        stat_str = f"Bias: {measures[0]}\nCorr: {measures[1]}\nRMSE: {measures[2]}\nNRMSE: {measures[3]}\nSkill: {measures[4]}"
        ax.annotate(stat_str, xy=(0.825, 0.06), 
                fontsize=8,
                xycoords="axes fraction",
                bbox={'boxstyle': 'square', 'facecolor': 'white', 'alpha': 0.75})
        plt.savefig(args.output/f"{d.station_id}.png", bbox_inches='tight', dpi=300)
        plt.close(fig)

        if args.tide:
            mt, ot = tidal_analysis(d)
            tide_plots(mt, ot, args.output, d.station_id)
            mt["station"] = d.station_id
            ot["station"] = d.station_id
            tidal_summary.append((mt, ot))

    # Close waterlevel datasets
    for wl in waterlevels:
        wl.close()

    #Filter summary and tidal_summary (if necessary) by skill
    summary_df = pd.DataFrame(summary, columns=['station_id', 'bias', 'corr', 'rmse', 'nrmse', 'skill'])
    summary_df["idx"] = list(range(len(summary)))
    #summary_df = summary_df.groupby('station_id').apply(lambda x: x.iloc[x.skill.argmax()])
    sel_idx = set(summary_df["idx"])
    summary_df.drop(columns=["idx"]).to_csv(args.output/"summary.csv", index=False)

    if args.tide:
        model_df = pd.concat([x[0] for x in map(tidal_summary.__getitem__, sel_idx)]).reset_index()
        obs_df = pd.concat([x[1] for x in map(tidal_summary.__getitem__, sel_idx)]).reset_index()

        amplitude_plot(model_df, obs_df, args.output)
        tidal_error(model_df, obs_df, args.output)


def get_options():
    parser = argparse.ArgumentParser()
    
    parser.add_argument('model', type=pathlib.Path, help='Model timeseries file/directory')
    parser.add_argument('--output', default=pathlib.Path(), type=pathlib.Path, help="Output directory")
    parser.add_argument('--cut', default=12, type=int, help='Number of hours to remove from head of timeseries')
    parser.add_argument('-t', '--tide', action='store_true', default=False, help='Solve tidal for tidal constituents')
    parser.add_argument('-b', '--bias-correct', action='store_true', help="Bias correct all stations")
    parser.add_argument('-s', '--storm', default=['Any'], action='append', help="Storm filter")
    parser.add_argument("--obs", type=pathlib.Path, required=True, help="data folder")
    parser.add_argument("--correspond", type=pathlib.Path, required=True, help='Data correspondence table')
    args = parser.parse_args()

    # Determine type based on detected filename patterns
    if args.model.is_dir():
        is_dflow = list(args.model.glob("FlowFM_*_his.nc"))
        is_schism = list(args.model.glob("out2d*.nc"))
        if is_dflow:
            args.model = is_dflow
            args.model_type = "dflow"
        elif is_schism:
            args.model = is_schism
            args.model_type = "schism"
        else:
            raise RuntimeError("Uknown filename pattern for model")
    else:
        if args.model.match("FlowFM_*_his.nc"):
            args.model_type = "dflow"
            args.model = [args.model]
        elif args.model.match("out2d*.nc"):
            args.model_type = "schism"
            args.model = [args.model]
        else:
            raise RuntimeError("Unknown filename pattern for model")

    if not have_pytides and args.tide:
        raise RuntimeError("PyTides needs to be installed for tidal constituent solving")
    return args


if __name__ == "__main__":
    args = get_options()
    main(args)               
            
            