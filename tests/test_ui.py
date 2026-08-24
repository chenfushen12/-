from inventory_tracker.importers import preview_template
from inventory_tracker.ui import format_import_previews


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
