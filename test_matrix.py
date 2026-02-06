# core/test_matrix.py
from typing import Iterator, Tuple
from config import TEST_BW_LIST, TEST_MCS_TABLE


def iter_bw_mcs() -> Iterator[Tuple[int, int]]:
    """
    Yield (bw, mcs) pairs with BW-aware MCS legality.

    Example:
      BW=20 -> MCS 8..0
      BW=40 -> MCS 9..0
      BW=80 -> MCS 9..0
    """
    for bw in TEST_BW_LIST:
        mcs_list = TEST_MCS_TABLE.get(bw)
        if not mcs_list:
            raise ValueError(f"No MCS table defined for BW={bw}")

        for mcs in mcs_list:
            yield bw, mcs
