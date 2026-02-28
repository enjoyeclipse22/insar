# InSAR Processor System

面向对象的InSAR数据处理系统，支持数据下载、处理和结果生成。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    InSAR Processor System                    │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐    ┌─────────────────┐                │
│  │  Data Downloader │    │    Processor     │                │
│  │  insar_downloader│───▶│  insar_processor │                │
│  │      .py        │    │      .py        │                │
│  └─────────────────┘    └─────────────────┘                │
│           │                      │                          │
│           ▼                      ▼                          │
│  ┌─────────────────┐    ┌─────────────────┐                │
│  │ DownloadConfig  │    │  InSARConfig    │                │
│  │  - 空间范围      │    │  - 路径配置      │                │
│  │  - 时间范围      │    │  - 处理参数      │                │
│  │  - 卫星参数      │    │  - 地震位置      │                │
│  └─────────────────┘    └─────────────────┘                │
└─────────────────────────────────────────────────────────────┘
```

## 核心模块

### 1. 数据下载器 (`insar_downloader.py`)

```python
from insar_downloader import DownloadConfig, InSARDownloader

# 配置下载参数
config = DownloadConfig(
    lon_min=36.5, lon_max=38.5,    # 空间范围
    lat_min=36.0, lat_max=38.5,
    start_date='2023-01-01',       # 时间范围
    end_date='2023-03-01',
    polarization='VV'              # 极化方式
)

# 创建下载器并下载
downloader = InSARDownloader(config)
results = downloader.download_all()  # 下载SLC、DEM、Landmask
```

**功能:**
- 根据经纬度范围下载Sentinel-1 SLC数据
- 下载SRTM3 DEM数据
- 生成陆地掩膜
- 验证数据完整性

### 2. 数据处理器 (`insar_processor.py`)

```python
from insar_processor import InSARConfig, InSARProcessor

# 配置处理参数
config = InSARConfig(
    work_dir='raw_kahr',
    data_dir='data_proc',
    results_dir='results',
    polarization='VV',
    resolution=180.0,
    earthquake_locations=[('M7.8', 37.217, 37.032)]
)

# 创建处理器并运行
processor = InSARProcessor(config)
processor.run_full_pipeline()  # 运行完整流程
```

**功能:**
- 面向对象设计，支持流畅接口
- 18个处理步骤，生成13张图像
- 支持分步执行
- 文件命名格式：`YYYYMMDD_位置_类型序号.jpg`

### 3. 配置文件共享

两个模块通过配置类共享参数：

| 参数 | DownloadConfig | InSARConfig |
|------|---------------|-------------|
| 数据目录 | `data_dir` | `data_dir` |
| 极化方式 | `polarization` | `polarization` |
| 空间范围 | `lon_min/max`, `lat_min/max` | - |
| 地震位置 | - | `earthquake_locations` |

## 快速开始

### 步骤1: 测试模块

```bash
# 测试下载器
python test_downloader.py

# 测试处理器
python test_processor.py
```

### 步骤2: 查看示例

```bash
# 查看完整工作流示例
python example_workflow.py
```

### 步骤3: 下载数据

```python
# download_script.py
from insar_downloader import DownloadConfig, InSARDownloader

config = DownloadConfig(
    lon_min=37.0, lon_max=38.0,
    lat_min=37.0, lat_max=38.0,
    start_date='2023-01-20',
    end_date='2023-02-20'
)

downloader = InSARDownloader(config)
downloader.download_all()  # 下载所有数据
```

### 步骤4: 处理数据

```python
# process_script.py
from insar_processor import InSARConfig, InSARProcessor

config = InSARConfig(
    data_dir='data_proc',
    results_dir='results'
)

processor = InSARProcessor(config)
processor.run_full_pipeline()  # 生成13张图像
```

## 输出文件

处理完成后，结果保存在 `results/` 目录：

```
YYYYMMDD_LocationPrefix_场景位置01.jpg
YYYYMMDD_LocationPrefix_场景位置DEM02.jpg
YYYYMMDD_LocationPrefix_WGS84地形03.jpg
YYYYMMDD_LocationPrefix_干涉相位04.jpg
YYYYMMDD_LocationPrefix_相干系数05.jpg
YYYYMMDD_LocationPrefix_陆地掩膜06.jpg
YYYYMMDD_LocationPrefix_掩膜后相位07.jpg
YYYYMMDD_LocationPrefix_解缠相位08.jpg
YYYYMMDD_LocationPrefix_去趋势相位09.jpg
YYYYMMDD_LocationPrefix_LOS位移10.jpg
YYYYMMDD_LocationPrefix_入射角11.jpg
YYYYMMDD_LocationPrefix_垂直位移12.jpg
YYYYMMDD_LocationPrefix_东西位移13.jpg
```

**示例:** `20260227_EQTurkeyM78_干涉相位04.jpg`

## 设计特点

### 1. 面向对象设计
- `InSARDownloader`: 封装下载逻辑
- `InSARProcessor`: 封装处理逻辑
- `DownloadConfig` / `InSARConfig`: 配置数据类

### 2. 设计模式
- **流畅接口**: 支持链式调用 `.step1().step2()`
- **模板方法**: `run_full_pipeline()` 定义标准流程
- **策略模式**: `run_steps()` 灵活执行指定步骤
- **依赖注入**: 配置通过构造函数传入

### 3. 参数外部化
所有参数通过配置类传递，无需修改代码：
```python
config = InSARConfig(
    resolution=90.0,              # 修改分辨率
    detrend_wavelength=50000,     # 修改去趋势波长
    snaphu_tiles_row=8,           # 修改SNAPHU分块
)
```

## 文件结构

```
kimi-insar/
├── insar_downloader.py      # 数据下载模块
├── insar_processor.py       # 数据处理模块
├── test_downloader.py       # 下载器测试
├── test_processor.py        # 处理器测试
├── example_workflow.py      # 完整工作流示例
├── run_insar_class.py       # 类方式运行脚本
├── data_proc/               # 输入数据目录
│   ├── *.SAFE/              # SLC数据
│   ├── dem.nc               # DEM文件
│   └── landmask.nc          # 陆地掩膜
├── raw_kahr/                # 工作目录
└── results/                 # 输出结果目录
    └── *.jpg                # 生成的图像
```

## 系统要求

- Python 3.8+
- PyGMTSAR
- Dask[distributed]
- XArray
- GeoPandas
- Matplotlib

## 注意事项

1. **数据下载**: 需要NASA Earthdata账号或ASF账号
2. **磁盘空间**: 单个SLC场景约2-4GB
3. **内存要求**: 建议8GB以上内存
4. **处理时间**: 完整流程约30-60分钟

## 许可证

MIT License
