# from analyze import *
import matplotlib
matplotlib.use('AGG')
import matplotlib.pyplot as plt
import pdb
from command import run
from datetime import timedelta
from glob import glob
from os import path
import numpy as np
import os
import feather
import pandas as pd
from pylab import rcParams


rcParams['figure.figsize'] = 10, 8


# DATA_DIR = './data/2020-05-09/'
DATA_DIR = './data/2020-06-01/'
# DATA_DIR = './data/pcc'

LOCAL = '192.168.1.102'
RECEIVER = LOCAL


def all_pcaps(data_dir=DATA_DIR):
    for dir in glob(f"{data_dir}/*"):
        if (not path.isdir(dir)):
            continue

        local = glob(f"{dir}/local.pcap")
        remote = [f for f in filter(lambda fname: not "local" in fname,
                                    glob(f"{dir}/*.pcap"))]
        remote = remote[0] if remote else None
        local = local[0] if local else None

        yield((local, remote, dir))


def tshark_all():
    pcaps = [local for local, _, _ in all_pcaps()]

    procs = []
    for pcap_file in pcaps:
        output_file = path.splitext(pcap_file)[0] + '.csv'

        if path.exists(output_file):
            os.remove(output_file)

        print('parsing pcap ', pcap_file)
        proc = run(tshark_command(pcap_file, output_file))
        procs.append((output_file, proc))

        if (len(procs) > 4):
            print("waiting for current 4 to finish")
            for output_file, proc in procs:
                proc.wait()
            procs.clear()

    for name, proc in procs:
        print('waiting for ', name)
        proc.wait()


def tshark_command(pcap_file, output_file):
    return f"""
    nice tshark -r {pcap_file} \
        -T fields  \
        -e frame.number  \
        -e frame.time_epoch  \
        -e eth.src  \
        -e eth.dst  \
        -e ip.src  \
        -e ip.dst  \
        -e tcp.srcport \
        -e tcp.dstport \
        -e tcp.seq \
        -e tcp.analysis.ack_rtt \
        -e ip.proto  \
        -e frame.time \
        -E header=y  \
        -E separator=,  \
        -E quote=d  \
        -E occurrence=f \
        > {output_file}
        """


def pcap_to_csv(pcap_file='./data/pcap.pcap', reparse=False):
    """
    TODO this is optional
    """
    output_file = path.splitext(pcap_file)[0] + '.csv'

    command = tshark_command(pcap_file, output_file)

    if reparse or not path.exists(output_file):
        print(f"regenerating {output_file}")
        run(command).wait()
    return output_file


def select_data_flow(groups):
    max_packets = 0
    max_group = pd.DataFrame()
    for _name, group in groups:
        group = group.reset_index()
        packets = len(group.index)
        if packets > max_packets:
            max_packets = packets
            max_group = group
    return max_group


def update_dataframe(df, filename):
    base, _ext = path.splitext(filename)
    feather_file = base + '.feather'
    feather.write_dataframe(df, feather_file)
    return df


def load_dataframe(filename, reparse=False):
    """
    opens csvfile and writes out .feather file

    return: dataframe
    """
    base, _ext = path.splitext(filename)
    feather_file = base + '.feather'
    if (path.isfile(feather_file) and not reparse):
        return feather.read_dataframe(feather_file)
    else:
        print(f"regenerating feather file for {filename}")
        df = pd.read_csv(filename)
        df['time'] = pd.to_datetime(
            df['frame.time'], infer_datetime_format=True)
        feather.write_dataframe(df, feather_file)
        return df


def parsed_filenames(filename):
    base, _ext = path.splitext(filename)
    receiver_path = base + '_receiver.feather'
    sender_path = base + '_sender.feather'
    return sender_path, receiver_path


def parse_csv(filename, reparse=False):
    """
    return pandas version of the csv
    """
    sender_path, receiver_path = parsed_filenames(filename)

    if (path.isfile(receiver_path) and path.isfile(sender_path) and not reparse):
        receiver_flow = feather.read_dataframe(receiver_path)
        sender_flow = feather.read_dataframe(sender_path)
        return (sender_flow, receiver_flow)

    df = load_dataframe(filename)

    # df.columns[df[.columns != 'tcp.analysis.ack_rtt']
    requried_columns = ['frame.time', 'frame.number', 'frame.time_epoch', 'eth.src', 'eth.dst',
                        'ip.src', 'ip.dst', 'tcp.srcport', 'tcp.dstport', 'tcp.seq', 'ip.proto', 'time']
    df.dropna(subset=requried_columns, inplace=True)
    df = df.set_index('frame.time').sort_index()

    group_tuple = ["ip.src", "ip.dst", "tcp.srcport", "tcp.dstport"]

    sender_traffic = df[df['ip.src'] != LOCAL].groupby(group_tuple)
    sender_flow = select_data_flow(sender_traffic)
    feather.write_dataframe(sender_flow, sender_path)

    receiver = df[df['ip.src'] == RECEIVER].groupby(group_tuple)
    receiver_flow = select_data_flow(receiver)
    feather.write_dataframe(receiver_flow, receiver_path)

    return (sender_flow, receiver_flow)


def parse_directory(directory):
    """
    expect directory like "data/2020-05-02/mlc1_cubic_54"
    """
    base = os.path.basename(directory)
    parts = base.split('_')
    host = parts[0]
    protocol = 'cubic'
    if len(parts) > 2:
        protocol = parts[1]
    return host, protocol


def summary(df, directory=None, start_bytes=0, end_bytes=1e9 * 10):
    """
    params:
        start_bytes: first seq to process
        end_bytes: last byte to process. Default 10 gig (so as not to be important)
    """

    temp_df = df[df['tcp.seq'] >= start_bytes][df['tcp.seq'] <= end_bytes].reset_index()

    if temp_df.empty:
        temp_df = df.tail(int(len(df) / 2)).reset_index()

    df = temp_df

    if (directory):
        host, protocol = parse_directory(directory)
    else:
        host, protocol = '', ''

    if df.empty:
        return {}, {}, host, protocol, 0

    start_time = df['frame.time'][0]

    def total(key): return df[key].max() - df[key].min()

    total_time = total('time')
    total_bytes = total('tcp.seq')
    throughput_mbps = (total_bytes / total_time.seconds) / 125000

    df['second'] = df.time.dt.minute * 60 + df.time.dt.second
    seconds = df[df.second > df.second.min() + 1][df.second < df.second.max() - 1] .groupby('second')
    mbps = (seconds['tcp.seq'].max() - seconds['tcp.seq'].min()) / 125000

    quantile_cutoffs = [
        0,
        .1,
        0.25,
        0.5,
        0.75,
        0.9,
        1.0
    ]

    throughput_quantiles = dict([(str(q), mbps.quantile(q))
                                 for q in quantile_cutoffs])
    throughput_quantiles["mean"] = throughput_mbps

    rtt_quantiles = dict([(str(q), df['tcp.analysis.ack_rtt'].quantile(
        q) * 1000) for q in quantile_cutoffs])
    throughput_quantiles["mean"] = df['tcp.analysis.ack_rtt'].mean() * 1000

    if directory:
        summary = f"""\
        ----------------------
        total time: {total_time}
        total bytes: {total_bytes}
        tp (mbps): {throughput_mbps}
        throughput quantiles: {throughput_quantiles}
        rtt quantiles: {throughput_quantiles}
        ----------------------
        """
        # print(summary)


    return throughput_quantiles, rtt_quantiles, host, protocol, start_time


def analyze(local, remote, dir):
    should_reparse = False
    should_reparse_feather = False

    print(local, remote)

    local_csv = pcap_to_csv(local, reparse=should_reparse)
    local_sender_flow, _local_receiver_flow = parse_csv(
        local_csv, reparse=should_reparse_feather)

    remote_csv = pcap_to_csv(remote, reparse=should_reparse)
    remote_sender_flow, _remote_receiver_flow = parse_csv(
        remote_csv, reparse=should_reparse_feather)
    second_half = {"start_bytes": 1e9 / 2, "end_bytes": 1e9}
    return [
            summary(local_sender_flow, dir),  # 0
            summary(local_sender_flow, dir, **second_half),
            summary(remote_sender_flow, dir),  # 2
            summary(remote_sender_flow, dir, **second_half),
        ]


def main(DATA_DIR=DATA_DIR):
    throughputs = []
    steady_throughputs = []
    rtts = []
    steady_rtts = []
    timeslices = []
    i = 0
    for local, remote, dir in all_pcaps():
        print(f"{i}: {local}, {remote}")
        i += 1
        if remote == None:
            print('ignoring trial', i)
            continue

        results = analyze(local, remote, dir)
        throughput_quantiles, _, host, protocol, start_time = results[0]
        steady_throughput_quantiles, _, _, _, steady_start_time = results[1]
        _, rtt_quantiles, _, _, _ = results[2]
        _, steady_rtt_quantiles, _, _, _ = results[3]

        throughput_quantiles['host'] = host
        throughput_quantiles['protocol'] = protocol
        throughput_quantiles['start_time'] = start_time
        throughputs.append(throughput_quantiles)

        steady_throughput_quantiles['host'] = host
        steady_throughput_quantiles['protocol'] = protocol
        steady_throughput_quantiles['start_time'] = start_time
        steady_throughputs.append(steady_throughput_quantiles)

        rtt_quantiles['host'] = host
        rtt_quantiles['protocol'] = protocol
        rtt_quantiles['start_time'] = start_time
        rtts.append(rtt_quantiles)

        steady_rtt_quantiles['host'] = host
        steady_rtt_quantiles['protocol'] = protocol
        steady_rtt_quantiles['start_time'] = steady_start_time
        steady_rtts.append(steady_rtt_quantiles)

        ts = timeslice(local)
        timeslices.append(pd.DataFrame(ts))

    df = pd.DataFrame(throughputs)
    df.to_csv(f"{DATA_DIR}/quantiles.csv")

    df = pd.DataFrame(steady_throughputs)
    df.to_csv(f"{DATA_DIR}/steady_quantiles.csv")

    df = pd.DataFrame(rtts)
    df.to_csv(f"{DATA_DIR}/rtt_quantiles.csv")

    df = pd.DataFrame(steady_rtts)
    df.to_csv(f"{DATA_DIR}/steady_rtt_quantiles.csv")

    ts = pd.concat(timeslices)
    ts.to_csv(f"{DATA_DIR}/timeslices.csv")

    # pdb.set_trace() # TODO: remove this, blocks at end of main


def timeslice(filename):
    """
    determine the time it takes to 
    """
    dirname = path.dirname(filename)
    sender_path, receiver_path = parsed_filenames(filename)
    sender = feather.read_dataframe(sender_path)
    gig = 1e+9
    num_objects = 100
    times = []
    base_time = sender.time.min()
    host, protocol = parse_directory(dirname)

    for i in range(num_objects):
        bytes_to_download = (i + 1) * (gig / num_objects)
        time_to_download = sender[sender['tcp.seq'] >
                                  bytes_to_download].iloc[0].time - base_time
        times.append({
            'file_size': bytes_to_download, 'time': time_to_download, 'protocol': protocol, 'host': host
        })

    df = pd.DataFrame(times)
    df.to_feather(dirname + '/timeslice.feather')
    return df


def retrofit_times(directory):
    """
    doesn't give good data...
    """
    directory = './data/2020-05-05/'
    start_times = []
    dirs = [dir for (_, _, dir) in all_pcaps()]
    csvfile = f'{directory}/quantiles.csv'
    quantiles_df = load_dataframe(csvfile)
    for d in dirs:
        data_df = pd.read_csv(f'{d}/local.csv', nrows=2)
        start_time = data_df['frame.time'][0]
        start_times.append(start_time)

    quantiles_df['start_time'] = start_times
    quantiles_df.to_csv(csvfile)


if __name__ == "__main__":
    main()
