# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_damaged_files.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A plan or an audit file that is damaged or in another encoding."""
from __future__ import annotations

import pytest

from hydra_umc_datalake.plan import PlanError, load_plan


@pytest.mark.parametrize("raw", [b"\xff\xfe{\x00", b'{"note": "se\xf1al"}', b"{ cut"])
def test_a_plan_file_that_cannot_be_decoded_is_a_message(tmp_path, raw):
    path = tmp_path / "plan.json"
    path.write_bytes(raw)
    with pytest.raises(PlanError, match="not valid JSON"):
        load_plan(path)
