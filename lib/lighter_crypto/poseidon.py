# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | No AI was harmed making this

from collections.abc import Iterable

from .field import GOLDILOCKS_ORDER, Fp5, make_fp5

WIDTH = 12
RATE = 8

# Lighter uses the same Poseidon2 permutation through two Go field backends. These protocol
# constants are from poseidon_crypto's Goldilocks width-12, x^7 configuration.
EXTERNAL_CONSTANTS = (
    (
        15492826721047263190,
        11728330187201910315,
        8836021247773420868,
        16777404051263952451,
        5510875212538051896,
        6173089941271892285,
        2927757366422211339,
        10340958981325008808,
        8541987352684552425,
        9739599543776434497,
        15073950188101532019,
        12084856431752384512,
    ),
    (
        4584713381960671270,
        8807052963476652830,
        54136601502601741,
        4872702333905478703,
        5551030319979516287,
        12889366755535460989,
        16329242193178844328,
        412018088475211848,
        10505784623379650541,
        9758812378619434837,
        7421979329386275117,
        375240370024755551,
    ),
    (
        3331431125640721931,
        15684937309956309981,
        578521833432107983,
        14379242000670861838,
        17922409828154900976,
        8153494278429192257,
        15904673920630731971,
        11217863998460634216,
        3301540195510742136,
        9937973023749922003,
        3059102938155026419,
        1895288289490976132,
    ),
    (
        5580912693628927540,
        10064804080494788323,
        9582481583369602410,
        10186259561546797986,
        247426333829703916,
        13193193905461376067,
        6386232593701758044,
        17954717245501896472,
        1531720443376282699,
        2455761864255501970,
        11234429217864304495,
        4746959618548874102,
    ),
    (
        13571697342473846203,
        17477857865056504753,
        15963032953523553760,
        16033593225279635898,
        14252634232868282405,
        8219748254835277737,
        7459165569491914711,
        15855939513193752003,
        16788866461340278896,
        7102224659693946577,
        3024718005636976471,
        13695468978618890430,
    ),
    (
        8214202050877825436,
        2670727992739346204,
        16259532062589659211,
        11869922396257088411,
        3179482916972760137,
        13525476046633427808,
        3217337278042947412,
        14494689598654046340,
        15837379330312175383,
        8029037639801151344,
        2153456285263517937,
        8301106462311849241,
    ),
    (
        13294194396455217955,
        17394768489610594315,
        12847609130464867455,
        14015739446356528640,
        5879251655839607853,
        9747000124977436185,
        8950393546890284269,
        10765765936405694368,
        14695323910334139959,
        16366254691123000864,
        15292774414889043182,
        10910394433429313384,
    ),
    (
        17253424460214596184,
        3442854447664030446,
        3005570425335613727,
        10859158614900201063,
        9763230642109343539,
        6647722546511515039,
        909012944955815706,
        18101204076790399111,
        11588128829349125809,
        15863878496612806566,
        5201119062417750399,
        176665553780565743,
    ),
)

INTERNAL_CONSTANTS = (
    11921381764981422944,
    10318423381711320787,
    8291411502347000766,
    229948027109387563,
    9152521390190983261,
    7129306032690285515,
    15395989607365232011,
    8641397269074305925,
    17256848792241043600,
    6046475228902245682,
    12041608676381094092,
    12785542378683951657,
    14546032085337914034,
    3304199118235116851,
    16499627707072547655,
    10386478025625759321,
    13475579315436919170,
    16042710511297532028,
    1411266850385657080,
    9024840976168649958,
    14047056970978379368,
    838728605080212101,
)

MATRIX_DIAGONAL = (
    0xC3B6C08E23BA9300,
    0xD84B5DE94A324FB6,
    0x0D0C371C5B35B84F,
    0x7964F570E7188037,
    0x5DAF18BBD996604B,
    0x6743BC47B9595257,
    0x5528B9362C59BB70,
    0xAC45E25B7127B68B,
    0xA2077D7DFBB606B5,
    0xF3FAAC6FAEE378AE,
    0x0C6388B51545E883,
    0xD27DBB6944917B60,
)


def _external_layer(state: list[int]) -> list[int]:
    mixed = []
    for i in range(0, WIDTH, 4):
        a, b, c, d = state[i : i + 4]
        ab = a + b
        cd = c + d
        total = ab + cd
        mixed.extend((total + ab + b, total + b + 2 * c, total + cd + d, total + d + 2 * a))

    sums = [sum(mixed[i::4]) for i in range(4)]
    return [(value + sums[i % 4]) % GOLDILOCKS_ORDER for i, value in enumerate(mixed)]


def _full_round(state: list[int], round_index: int) -> list[int]:
    constants = EXTERNAL_CONSTANTS[round_index]
    state = [
        pow(value + constant, 7, GOLDILOCKS_ORDER) for value, constant in zip(state, constants)
    ]
    return _external_layer(state)


def poseidon_permute(values: Iterable[int]) -> tuple[int, ...]:
    state = tuple(value % GOLDILOCKS_ORDER for value in values)
    if len(state) != WIDTH:
        raise ValueError(f"Poseidon2 state must have {WIDTH} elements, got {len(state)}")

    state = _external_layer(list(state))
    for round_index in range(4):
        state = _full_round(state, round_index)

    for constant in INTERNAL_CONSTANTS:
        state[0] = pow(state[0] + constant, 7, GOLDILOCKS_ORDER)
        total = sum(state)
        state = [
            (value * diagonal + total) % GOLDILOCKS_ORDER
            for value, diagonal in zip(state, MATRIX_DIAGONAL)
        ]

    for round_index in range(4, 8):
        state = _full_round(state, round_index)

    return tuple(state)


def poseidon_hash(values: Iterable[int], outputs: int = 5) -> tuple[int, ...]:
    if outputs <= 0:
        raise ValueError("Poseidon2 output count must be positive")

    values = tuple(value % GOLDILOCKS_ORDER for value in values)
    state = (0,) * WIDTH
    for i in range(0, len(values), RATE):
        state = (*values[i : i + RATE], *state[len(values[i : i + RATE]) :])
        state = poseidon_permute(state)

    result = []
    while len(result) < outputs:
        result.extend(state[:RATE])
        if len(result) < outputs:
            state = poseidon_permute(state)

    return tuple(result[:outputs])


def poseidon_hash_fp5(values: Iterable[int]) -> Fp5:
    return make_fp5(poseidon_hash(values, 5))


def bytes_to_fields(data: bytes) -> tuple[int, ...]:
    fields = []
    for i in range(0, len(data), 8):
        value = int.from_bytes(data[i : i + 8].ljust(8, b"\0"), "little")
        if value >= GOLDILOCKS_ORDER:
            raise ValueError("byte chunk is not a canonical Goldilocks field element")

        fields.append(value)

    return tuple(fields)
