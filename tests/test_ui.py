from inventory_tracker.importers import preview_code_mapping, preview_template
from inventory_tracker.ui import IMPORT_FIELDS, format_import_previews


def test_import_preview_display_shows_file_and_column_but_not_hash(tmp_path) -> None:
    path = tmp_path / "商品主表.xlsx"
    import pandas as pd

    pd.DataFrame([{"GROUPCODE": "G1", "货品编号": 123}]).to_excel(path, index=False)
    preview = preview_template(path)

    text = format_import_previews((preview,))

    assert "商品主模板" in text
    assert "商品主表.xlsx" in text
    assert "第 2 列" in text
    assert "货品编号" in text
    assert "文件哈希" not in text
    assert preview.file_hash[:12] not in text


def test_import_ui_includes_optional_code_mapping_and_preview_label(tmp_path) -> None:
    path = tmp_path / "新旧编码对应表.xlsx"
    import pandas as pd

    pd.DataFrame([{"老编号": "OLD", "新编号": "NEW", "名字": "新组"}]).to_excel(path, index=False)

    text = format_import_previews((preview_code_mapping(path),))

    assert ("code_mapping", "新旧编码替换表（可选）") in IMPORT_FIELDS
    assert "新旧编码替换表" in text
    assert "读取 1 行" in text
