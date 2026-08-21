# 库存跟踪监控

本项目是一个本地单用户 Tkinter 库存跟踪工具，支持商品主模板、销售数据、北京库存和星望库存导入，SQLite 快照、MOH/环比计算、库存预警和 Excel 导出。

## 启动

在项目根目录执行：

```powershell
venv\Scripts\python.exe main.py
```

也可以双击 `run.bat`。

首次安装依赖：

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
```

运行测试：

```powershell
venv\Scripts\python.exe -m pytest -q
```

应用数据、SQLite 数据库、配置和原始导入文件会保存到 `data/`；该目录默认不提交到 Git。
