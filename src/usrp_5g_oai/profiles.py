"""Named radio profiles plus the X310/UBX numbers the safety checks rely on.

The PHY itself is never invented here: each profile names a pinned upstream OAI
template whose servingCellConfigCommon is copied verbatim at render time. The
frequencies below are *expected* values used for validation before rendering;
render() re-reads the template and refuses to continue if they disagree.
"""
import math

# Every profile below is single-carrier, SISO, intra-frequency (both cells share it).
PROFILES = {
    # 40 MHz TDD. 61.44 MS/s per radio: needs a 10 GbE link per X310.
    'n78_106prb_30khz': {
        'phy_template': 'du', 'band': 78, 'duplex': 'TDD', 'scs_khz': 30, 'numerology': 1, 'prb': 106,
        'sample_rate_sps': 61_440_000, 'channel_bandwidth_hz': 40_000_000, 'ssb_lmax': 8,
        'ssb_arfcn': 630048, 'dl_point_a_arfcn': 628776, 'ul_point_a_arfcn': None,
        'dl_band_mhz': (3300, 3800), 'ul_band_mhz': (3300, 3800)},
    # 10 MHz TDD. 15.36 MS/s per radio: one dedicated 1 GbE link per X310 is enough.
    'n78_24prb_30khz': {
        'phy_template': 'phy_n78_24prb', 'band': 78, 'duplex': 'TDD', 'scs_khz': 30, 'numerology': 1, 'prb': 24,
        'sample_rate_sps': 15_360_000, 'channel_bandwidth_hz': 10_000_000, 'ssb_lmax': 8,
        'ssb_arfcn': 640320, 'dl_point_a_arfcn': 640032, 'ul_point_a_arfcn': None,
        'dl_band_mhz': (3300, 3800), 'ul_band_mhz': (3300, 3800)},
    # 5 MHz FDD (DL 2152.68 MHz / UL 1752.68 MHz). 7.68 MS/s per radio: both X310s fit on ONE 1 GbE link.
    # PHY from OAI's X300 hardware template, not the RFsim one.
    'n66_25prb_15khz': {
        'phy_template': 'phy_n66_25prb', 'band': 66, 'duplex': 'FDD', 'scs_khz': 15, 'numerology': 0, 'prb': 25,
        'sample_rate_sps': 7_680_000, 'channel_bandwidth_hz': 5_000_000, 'ssb_lmax': 4,
        'ssb_arfcn': 430590, 'dl_point_a_arfcn': 430086, 'ul_point_a_arfcn': 350086,
        'dl_band_mhz': (2110, 2200), 'ul_band_mhz': (1710, 1780)},
}

# Sample rates the pinned radio/USRP/usrp_lib.cpp accepts for X300/X310 (anything else exits).
# 11.52 and 5.76 MS/s (three-quarter sampling, OAI option -E) are NOT in that list, so -E is never used.
X300_RATES_SPS = (245_760_000, 184_320_000, 122_880_000, 92_160_000, 61_440_000, 46_080_000,
                  30_720_000, 23_040_000, 15_360_000, 7_680_000, 1_920_000)
X300_MASTER_CLOCK_HZ = 184.32e6

# radio/USRP/usrp_lib.cpp @ pin, calib_table_x310: RX gain offset by nearest frequency.
# OAI requests UHD RX gain = max_rxgain - att_rx - offset (bandwidth adjustment is off for X300).
X310_RX_CAL = ((3500e6, 77.0), (2660e6, 81.0), (2300e6, 81.0), (1880e6, 82.0), (816e6, 85.0))

# Ettus UBX/UBX-160 (kb.ettus.com/UBX, Ettus UBX datasheet) - see docs/SOURCES.md S10.
UBX = {'tx_gain_max_db': 31.5, 'rx_gain_max_db': 31.5, 'gain_step_db': 0.5,
       'max_input_dbm': -15.0,          # receiver damage ceiling, NOT an operating point
       'max_tx_dbm': 20.0}              # 10 MHz-3 GHz: 20 dBm; 3-6 GHz: 8-20 dBm (20 used as the bound)

WIRE_BYTES_PER_SAMPLE = 4               # sc16 over the wire, hard-coded in usrp_lib.cpp at the pin
ETHERNET_OVERHEAD = 1.06                # CHDR+UDP+IP+Ethernet framing at a 1500-byte MTU (~1.056)


def arfcn_to_hz(n):
    """3GPP TS 38.104 section 5.4.2.1 global frequency raster."""
    if n < 600000:
        return n * 5e3
    if n < 2016667:
        return 3000e6 + (n - 600000) * 15e3
    return 24250.08e6 + (n - 2016667) * 60e3


def _prof(p):
    """A profile name or an (optionally shifted) profile dict."""
    return PROFILES[p] if isinstance(p, str) else p


def sync_raster_step_hz(freq_hz):
    """TS 38.104 5.4.3.1 GSCN spacing: N x 1.2 MHz below 3 GHz, 1.44 MHz from 3 to 24.25 GHz."""
    return 1.2e6 if freq_hz < 3000e6 else 1.44e6


def hz_to_arfcn_delta(delta_hz, freq_hz):
    """ARFCN units for a frequency shift (5 kHz below 3 GHz, 15 kHz up to 24.25 GHz)."""
    step = 5e3 if freq_hz < 3000e6 else 15e3
    n = delta_hz / step
    assert abs(n - round(n)) < 1e-6, 'shift is not on the ARFCN grid'
    return int(round(n))


def shifted(name, dl_center_hz):
    """The profile moved to another carrier centre, keeping every PHY offset of the pinned template.

    SSB, Point A (DL and UL) move together by a whole number of synchronization-raster steps, so the SSB
    stays on the GSCN raster and offsetToPointA / k_SSB / the CORESET#0 position are unchanged. FDD keeps
    its duplex spacing. Raises ValueError with the nearest valid centres otherwise.
    """
    base = PROFILES[name]
    f0 = frequencies(name)
    delta = dl_center_hz - f0['dl_center_hz']
    step = sync_raster_step_hz(f0['ssb_hz'])
    k = round(delta / step)
    if abs(delta - k * step) > 1e3:
        lo = math.floor(delta / step)
        near = [round((f0['dl_center_hz'] + j * step) / 1e6, 4) for j in (lo, lo + 1)]
        raise ValueError(f'radio.dl_center_mhz {dl_center_hz/1e6:g} is not reachable from the {name} template in '
                         f'{step/1e6:g} MHz sync-raster steps; nearest valid centres: {near} MHz')
    p = dict(base)
    if k:
        d = hz_to_arfcn_delta(k * step, f0['ssb_hz'])
        p['ssb_arfcn'] = base['ssb_arfcn'] + d
        p['dl_point_a_arfcn'] = base['dl_point_a_arfcn'] + d
        if base['ul_point_a_arfcn'] is not None:
            p['ul_point_a_arfcn'] = base['ul_point_a_arfcn'] + hz_to_arfcn_delta(k * step, arfcn_to_hz(base['ul_point_a_arfcn']))
    p['shift_steps'], p['shift_hz'] = k, k * step
    f = frequencies(p)
    for side in ('dl', 'ul'):
        lo, hi = base[f'{side}_band_mhz']
        c, half = f[f'{side}_center_hz'], base['channel_bandwidth_hz'] / 2
        if not (lo * 1e6 <= c - half and c + half <= hi * 1e6):
            raise ValueError(f'{side.upper()} carrier {(c-half)/1e6:.3f}-{(c+half)/1e6:.3f} MHz leaves band n{base["band"]} '
                             f'({lo}-{hi} MHz)')
    return p


def frequencies(name):
    p = _prof(name)
    scs = p['scs_khz'] * 1e3
    half_bw = p['prb'] * 12 * scs / 2
    dl_a = arfcn_to_hz(p['dl_point_a_arfcn'])
    ul_a = arfcn_to_hz(p['ul_point_a_arfcn']) if p['ul_point_a_arfcn'] is not None else dl_a
    ssb = arfcn_to_hz(p['ssb_arfcn'])
    # nr-uesoftmodem --ssb: first SSB subcarrier counted from Point A (SSB centre is subcarrier 120).
    ssb_start = round((ssb - dl_a) / scs) - 120
    return {'dl_center_hz': dl_a + half_bw, 'ul_center_hz': ul_a + half_bw, 'ssb_hz': ssb,
            'ssb_start_subcarrier': ssb_start, 'occupied_bw_hz': 2 * half_bw}


def x310_rx_cal_offset(freq_hz):
    best, offset = math.inf, None
    for f, o in X310_RX_CAL:          # strict '<' keeps the first entry on ties, like usrp_lib.cpp
        if abs(freq_hz - f) < best:
            best, offset = abs(freq_hz - f), o
    return offset


def is_ubx(cell):
    return 'UBX' in str(cell.get('daughterboard', '')).upper()


def oai_gains(cell, profile_name):
    """What the pinned OAI will ask UHD for, derived from the RU fields (nr-ru.c + usrp_lib.cpp).
    profile_name: a profile name or the (possibly shifted) dict from config.profile(c)."""
    f = frequencies(profile_name)
    offset = x310_rx_cal_offset(f['ul_center_hz'])
    tx_max = cell.get('tx_gain_max_db')
    out = {'rx_freq_hz': f['ul_center_hz'], 'tx_freq_hz': f['dl_center_hz'], 'rx_cal_offset_db': offset,
           'uhd_rx_gain_db': cell['max_rxgain'] - cell['att_rx_db'] - offset,
           'uhd_tx_gain_db': None if tx_max is None else tx_max - cell['att_tx_db'],
           'tx_peak_bound_dbm': None}
    if out['uhd_tx_gain_db'] is not None and is_ubx(cell):
        # Upper bound on the connector output: full-scale CW at maximum gain, reduced by the backoff from max.
        out['tx_peak_bound_dbm'] = UBX['max_tx_dbm'] - (tx_max - out['uhd_tx_gain_db'])
    return out


def stream_mbps(sample_rate_sps):
    """One direction of one radio's single sc16 stream, including framing overhead."""
    return sample_rate_sps * WIRE_BYTES_PER_SAMPLE * 8 * ETHERNET_OVERHEAD / 1e6


def fspl_db(distance_m, freq_hz):
    """Free-space path loss (far field). Not valid for antennas a few centimetres apart."""
    return 20 * math.log10(4 * math.pi * distance_m * freq_hz / 299792458.0)
