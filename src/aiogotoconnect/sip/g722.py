"""Pure-Python G.722 (64 kbit/s) codec for wideband RTP audio.

Port of the ITU-T G.722 reference algorithm (as in ITU-T G.191 STL / spandsp):
a 24-tap QMF splits 16 kHz PCM into two 8 kHz sub-bands, the low band is coded
with 6-bit ADPCM and the high band with 2-bit ADPCM, and each output byte is
`(high << 6) | low` - one byte per two input samples.

The encoder is the product (the call path is send-only). The decoder exists
only so tests can verify the encoder by round-trip; nothing else uses it.
Integer arithmetic mirrors the reference bit-for-bit (Python `>>` on negatives
is arithmetic, like the C reference), so output matches other G.722 stacks.
"""

import struct

# Low band: 6-bit quantizer decision levels, and interval -> code for each sign.
Q6 = (0, 35, 72, 110, 150, 190, 233, 276, 323, 370, 422, 473, 530, 587, 650, 714,
      786, 858, 940, 1023, 1121, 1219, 1339, 1458, 1612, 1765, 1980, 2195, 2557, 2919, 0, 0)
ILN = (0, 63, 62, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19,
       18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 0)
ILP = (0, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47,
       46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 0)
# Low band: 4-bit inverse quantizer (predictor feedback), 6-bit inverse
# quantizer (decoder output), and log scale-factor adaptation.
QM4 = (0, -20456, -12896, -8968, -6288, -4240, -2584, -1200,
       20456, 12896, 8968, 6288, 4240, 2584, 1200, 0)
QM6 = (-136, -136, -136, -136, -24808, -21904, -19008, -16704,
       -14984, -13512, -12280, -11192, -10232, -9360, -8576, -7856,
       -7192, -6576, -6000, -5456, -4944, -4464, -4008, -3576,
       -3168, -2776, -2400, -2032, -1688, -1360, -1040, -728,
       24808, 21904, 19008, 16704, 14984, 13512, 12280, 11192,
       10232, 9360, 8576, 7856, 7192, 6576, 6000, 5456,
       4944, 4464, 4008, 3576, 3168, 2776, 2400, 2032,
       1688, 1360, 1040, 728, 432, 136, -432, -136)
RL42 = (0, 7, 6, 5, 4, 3, 2, 1, 7, 6, 5, 4, 3, 2, 1, 0)
WL = (-60, -30, 58, 172, 334, 538, 1198, 3042)
# High band: 2-bit quantizer (magnitude 1/2 -> code per sign), inverse
# quantizer, and log scale-factor adaptation.
IHN = (0, 1, 0)
IHP = (0, 3, 2)
QM2 = (-7408, -1616, 7408, 1616)
RH2 = (2, 1, 2, 1)
WH = (0, -214, 798)
# Shared: log -> linear scale-factor table, and half of the symmetric QMF taps.
ILB = (2048, 2093, 2139, 2186, 2233, 2282, 2332, 2383, 2435, 2489, 2543, 2599, 2656, 2714, 2774,
       2834, 2896, 2960, 3025, 3091, 3158, 3228, 3298, 3371, 3444, 3520, 3597, 3676, 3756, 3838,
       3922, 4008)
QMF = (3, -11, 12, 32, -210, 951, 3876, -805, 362, -156, 53, -11)


def _sat(x: int) -> int:
    """Clamp to int16, as the reference's saturate()."""
    return -32768 if x < -32768 else 32767 if x > 32767 else x


class _Band:
    """Adaptive predictor + scale-factor state for one sub-band (ITU blocks 3 and 4)."""

    def __init__(self, det: int, nb_max: int, shift: int):
        self.det, self.nb_max, self.shift = det, nb_max, shift
        self.s = self.sz = self.nb = 0
        self.r, self.p, self.a = [0] * 3, [0] * 3, [0] * 3
        self.d, self.b = [0] * 7, [0] * 7

    def scale(self, weight: int) -> None:
        """LOGSCL/LOGSCH then SCALEL/SCALEH: adapt the quantizer step size."""
        self.nb = min(max(((self.nb * 127) >> 7) + weight, 0), self.nb_max)
        wd1 = (self.nb >> 6) & 31
        wd2 = self.shift - (self.nb >> 11)
        self.det = (ILB[wd1] << -wd2 if wd2 < 0 else ILB[wd1] >> wd2) << 2

    def update(self, dx: int) -> None:
        """Block 4: reconstruct, adapt pole/zero predictor coefficients, predict next."""
        r0 = _sat(self.s + dx)
        p0 = _sat(self.sz + dx)
        p, a, d, b = self.p, self.a, self.d, self.b
        sg0, sg1, sg2 = p0 >> 15, p[1] >> 15, p[2] >> 15  # sign bits: 0 or -1

        # UPPOL2: second pole coefficient
        wd1 = _sat(a[1] << 2)
        wd2 = min(-wd1 if sg0 == sg1 else wd1, 32767)
        a2 = (128 if sg0 == sg2 else -128) + (wd2 >> 7) + ((a[2] * 32512) >> 15)
        a2 = min(max(a2, -12288), 12288)
        # UPPOL1: first pole coefficient, bounded by the second
        a1 = _sat((192 if sg0 == sg1 else -192) + ((a[1] * 32640) >> 15))
        lim = _sat(15360 - a2)
        a1 = min(max(a1, -lim), lim)
        # UPZERO: six zero coefficients
        step = 0 if dx == 0 else 128
        sgd = dx >> 15
        bp = [0] + [_sat((step if (d[i] >> 15) == sgd else -step) + ((b[i] * 32640) >> 15))
                    for i in range(1, 7)]

        # DELAYA: shift histories (reference writes slot 0 then shifts, so 0 and 1 match)
        self.d = [dx, dx] + d[1:6]
        self.b = bp
        self.r = [r0, r0, self.r[1]]
        self.p = [p0, p0, p[1]]
        self.a = [0, a1, a2]

        # FILTEP + FILTEZ + PREDIC: pole and zero predictor outputs
        sp = _sat(((a1 * _sat(r0 + r0)) >> 15) + ((a2 * _sat(self.r[2] + self.r[2])) >> 15))
        self.sz = _sat(sum((bp[i] * _sat(self.d[i] + self.d[i])) >> 15 for i in range(1, 7)))
        self.s = _sat(sp + self.sz)


def encode(pcm: bytes) -> bytes:
    """16-bit LE mono 16 kHz PCM -> G.722 64 kbit/s bytes (a trailing odd sample is dropped)."""
    count = len(pcm) // 4 * 2
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    low, high = _Band(32, 18432, 8), _Band(8, 22528, 10)
    x = [0] * 24
    out = bytearray()
    for j in range(0, count, 2):
        # Transmit QMF: push two samples, keep one output per sub-band
        x = x[2:] + [samples[j], samples[j + 1]]
        sumodd = sum(x[2 * i] * QMF[i] for i in range(12))
        sumeven = sum(x[2 * i + 1] * QMF[11 - i] for i in range(12))
        xlow = (sumeven + sumodd) >> 14
        xhigh = (sumeven - sumodd) >> 14

        # Low band: SUBTRA, QUANTL, INVQAL, adapt
        el = _sat(xlow - low.s)
        wd = el if el >= 0 else -(el + 1)
        i = 1
        while i < 30 and wd >= (Q6[i] * low.det) >> 12:
            i += 1
        ilow = ILN[i] if el < 0 else ILP[i]
        ril = ilow >> 2
        dlow = (low.det * QM4[ril]) >> 15
        low.scale(WL[RL42[ril]])
        low.update(dlow)

        # High band: SUBTRA, QUANTH, INVQAH, adapt
        eh = _sat(xhigh - high.s)
        wd = eh if eh >= 0 else -(eh + 1)
        mih = 2 if wd >= (564 * high.det) >> 12 else 1
        ihigh = IHN[mih] if eh < 0 else IHP[mih]
        dhigh = (high.det * QM2[ihigh]) >> 15
        high.scale(WH[RH2[ihigh]])
        high.update(dhigh)

        out.append((ihigh << 6) | ilow)
    return bytes(out)


def decode(data: bytes) -> bytes:
    """G.722 64 kbit/s bytes -> 16-bit LE mono 16 kHz PCM. Test/verification only."""
    low, high = _Band(32, 18432, 8), _Band(8, 22528, 10)
    x = [0] * 24
    samples = []
    for code in data:
        ilow, ihigh = code & 0x3F, code >> 6

        # Low band: output from the 6-bit inverse quantizer, predictor from the 4-bit one
        rlow = min(max(low.s + ((low.det * QM6[ilow]) >> 15), -16384), 16383)
        ril = ilow >> 2
        dlow = (low.det * QM4[ril]) >> 15
        low.scale(WL[RL42[ril]])
        low.update(dlow)

        # High band
        dhigh = (high.det * QM2[ihigh]) >> 15
        rhigh = min(max(high.s + dhigh, -16384), 16383)
        high.scale(WH[RH2[ihigh]])
        high.update(dhigh)

        # Receive QMF: two output samples per code
        x = x[2:] + [rlow + rhigh, rlow - rhigh]
        xout2 = sum(x[2 * i] * QMF[i] for i in range(12))
        xout1 = sum(x[2 * i + 1] * QMF[11 - i] for i in range(12))
        samples += (_sat(xout1 >> 11), _sat(xout2 >> 11))
    return struct.pack(f"<{len(samples)}h", *samples)
