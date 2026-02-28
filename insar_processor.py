#!/usr/bin/env python3
"""
InSAR Processor - 面向对象的InSAR图像处理类

设计模式：
- 策略模式：处理步骤可独立执行
- 模板方法模式：定义标准处理流程
- 依赖注入：配置参数通过构造函数传入
"""

import os
import sys
import numpy as np
import pandas as pd
import geopandas as gpd
import shapely.geometry
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any
from datetime import datetime
import logging
import warnings

warnings.filterwarnings('ignore')

from pygmtsar import S1, Stack, tqdm_dask, Tiles
from dask.distributed import Client
import dask
import shutil

# Monkey patch: 将pygmtsar的默认h5netcdf引擎替换为netcdf4
# 这解决了Tiles.download_dem()生成的DEM文件与h5netcdf不兼容的问题
_original_init = Stack.__init__
def _patched_init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    # 替换netcdf引擎
    if hasattr(self, 'netcdf_engine'):
        self.netcdf_engine = 'netcdf4'
Stack.__init__ = _patched_init


@dataclass
class InSARConfig:
    """InSAR处理配置类"""
    # 路径配置
    work_dir: str = 'raw_kahr'
    data_dir: str = 'data_proc'
    results_dir: str = 'results/turkey'  # 修改：保存到 turkey 子目录
    
    # 数据文件
    dem_file: str = 'dem.nc'
    landmask_file: str = 'landmask.nc'
    
    # 处理参数
    polarization: str = 'VV'
    resolution: float = 180.0
    
    # 多视和滤波参数
    multilook_wavelength: int = 400
    multilook_coarsen: Tuple[int, int] = (12, 48)
    goldstein_alpha: int = 16
    
    # 相位解缠参数 (SNAPHU)
    snaphu_tiles_row: int = 4
    snaphu_tiles_col: int = 4
    snaphu_overlap: int = 200
    
    # 去趋势参数 - 参考 notebook 使用 300km
    detrend_wavelength: int = 300000  # 300km，与 notebook 一致
    
    # 地震位置 (POI) - 土耳其地震默认位置
    earthquake_locations: List[Tuple[str, float, float]] = None  # [(name, lon, lat), ...]
    
    def __post_init__(self):
        if self.earthquake_locations is None:
            # 默认：土耳其 2023年地震位置 (M7.8 和 M7.5)
            # 注意：坐标格式为 (name, lon, lat)
            self.earthquake_locations = [
                ('M7.8', 37.217, 37.032),  # M7.8 地震位置
                ('M7.5', 37.028, 37.206),  # M7.5 地震位置
            ]
        
        # 检查DEM文件大小写 (使用绝对路径)
        from pathlib import Path
        data_path = Path(self.data_dir).resolve()
        dem_lower = data_path / 'dem.nc'
        dem_upper = data_path / 'DEM.nc'
        if not dem_lower.exists() and dem_upper.exists():
            self.dem_file = 'DEM.nc'
            print(f"  检测到DEM文件 (大写): {dem_upper}")
        
        # 检查Landmask文件大小写  
        lm_lower = data_path / 'landmask.nc'
        lm_upper = data_path / 'LANDMASK.nc'
        if not lm_lower.exists() and lm_upper.exists():
            self.landmask_file = 'LANDMASK.nc'
    
    @property
    def dem_path(self) -> str:
        return f'{self.data_dir}/{self.dem_file}'
    
    @property
    def landmask_path(self) -> str:
        return f'{self.data_dir}/{self.landmask_file}'


class InSARProcessor:
    """
    InSAR处理器主类
    
    使用示例:
        config = InSARConfig(
            work_dir='raw_kahr',
            data_dir='data_proc',
            polarization='VV',
            resolution=180.0
        )
        processor = InSARProcessor(config)
        processor.run_full_pipeline()
    """
    
    def __init__(self, config: InSARConfig):
        """
        初始化处理器
        
        Args:
            config: InSARConfig配置对象
        """
        self.config = config
        self._setup_logging()
        self._setup_matplotlib()
        
        # 初始化成员变量
        self.sbas: Optional[Stack] = None
        self.scenes = None
        self.client: Optional[Client] = None
        self.data = None
        self.topo = None
        self.corr = None
        self.intf = None
        self.unwrap = None
        self.detrend = None
        self._use_geographic = True  # 默认使用地理坐标系
        
        # 创建POI（在load_data之前就可以创建）
        self.poi: Optional[gpd.GeoDataFrame] = self._create_poi()
        
        # 生成文件名前缀 (日期_位置格式)
        self.file_prefix = self._generate_file_prefix()
        
        self.logger.info(f"InSAR处理器初始化完成")
        self.logger.info(f"文件前缀: {self.file_prefix}")
    
    def _setup_logging(self):
        """配置日志系统"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def _setup_matplotlib(self):
        """配置matplotlib参数"""
        plt.rcParams['figure.figsize'] = [12, 4]
        plt.rcParams['figure.dpi'] = 100
    
    def _generate_file_prefix(self) -> str:
        """
        生成文件命名前缀
        格式: YYYYMMDD_地点描述_
        """
        # 从数据目录扫描获取日期
        today = datetime.now()
        date_str = today.strftime('%Y%m%d')
        
        # 位置描述 (可以根据POI计算中心点)
        if self.config.earthquake_locations:
            # 使用第一个地震位置作为标识
            first_loc = self.config.earthquake_locations[0]
            location_desc = f"EQ{first_loc[0]}"
        else:
            location_desc = "InSAR"
        
        return f"{date_str}_{location_desc}_"
    
    def _get_output_filename(self, description: str, index: int) -> str:
        """
        生成输出文件名
        
        Args:
            description: 图像描述 (中文或英文)
            index: 序号 (01, 02, ...)
        
        Returns:
            完整文件名
        """
        # 清理描述，移除特殊字符
        clean_desc = description.replace(' ', '_').replace('/', '_')
        return f"{self.file_prefix}{clean_desc}{index:02d}.jpg"
    
    def _create_poi(self) -> Optional[gpd.GeoDataFrame]:
        """创建兴趣点(地震位置)GeoDataFrame"""
        locations = self.config.earthquake_locations
        if locations is None or len(locations) == 0:
            return None
            
        lons = [loc[1] for loc in locations]
        lats = [loc[2] for loc in locations]
        names = [loc[0] for loc in locations]
        
        return gpd.GeoDataFrame(
            geometry=gpd.points_from_xy(lons, lats),
            data={'name': names},
            crs='EPSG:4326'
        )
    
    def initialize_dask(self) -> 'InSARProcessor':
        """初始化Dask分布式客户端"""
        self.logger.info("初始化Dask客户端...")
        from multiprocessing import freeze_support
        freeze_support()
        self.client = Client()
        self.logger.info(f"Dask客户端已启动: {self.client.dashboard_link}")
        return self
    
    def load_data(self) -> 'InSARProcessor':
        """加载SLC数据，支持多子条带场景自动合并"""
        self.logger.info("扫描SLC数据...")
        all_scenes = S1.scan_slc(
            self.config.data_dir, 
            polarization=self.config.polarization
        )
        self.logger.info(f"发现 {len(all_scenes)} 个SLC子条带")
        
        # 过滤：只使用VV极化，并选择中间子条带(IW2)或合并所有
        # 策略：如果有多子条带，使用IW2（中间那个）以获得最佳覆盖
        scenes_vv = all_scenes[all_scenes.polarization == 'VV']
        
        # 检查子条带情况
        if 'subswath' in scenes_vv.columns:
            subswaths = scenes_vv.subswath.unique()
            self.logger.info(f"VV极化子条带: {list(subswaths)}")
            
            if len(subswaths) > 1:
                # 选择子条带2（中间那个，通常覆盖最好）
                if 2 in subswaths:
                    self.scenes = scenes_vv[scenes_vv.subswath == 2]
                    self.logger.info(f"选择子条带2: {len(self.scenes)} 个场景")
                elif 'IW2' in subswaths:
                    self.scenes = scenes_vv[scenes_vv.subswath == 'IW2']
                    self.logger.info(f"选择IW2子条带: {len(self.scenes)} 个场景")
                else:
                    # 选择中间子条带
                    mid = subswaths[len(subswaths)//2]
                    self.scenes = scenes_vv[scenes_vv.subswath == mid]
                    self.logger.info(f"选择子条带{mid}: {len(self.scenes)} 个场景")
            else:
                self.scenes = scenes_vv
        else:
            self.scenes = scenes_vv
        
        # 检查是否需要合并多子条带（同一天多幅）
        unique_dates = self.scenes.index.unique()
        self.logger.info(f"唯一日期数: {len(unique_dates)}, 场景数: {len(self.scenes)}")
        
        # 初始化SBAS栈
        self.logger.info("初始化SBAS堆栈...")
        results_path = Path(self.config.results_dir)
        results_path.mkdir(exist_ok=True)
        
        # 备份已有的 PRM/LED 文件（如果存在）
        work_path = Path(self.config.work_dir)
        backup_files = []
        if work_path.exists():
            for ext in ['*.PRM', '*.LED']:
                backup_files.extend(list(work_path.glob(ext)))
            if backup_files:
                self.logger.info(f"  备份 {len(backup_files)} 个文件...")
                import tempfile
                backup_dir = tempfile.mkdtemp()
                for f in backup_files:
                    shutil.copy2(f, backup_dir)
        
        # 创建 Stack（允许删除已有目录）
        self.sbas = Stack(
            self.config.work_dir, 
            drop_if_exists=True
        ).set_scenes(self.scenes)
        
        # 恢复备份的文件
        if backup_files:
            self.logger.info(f"  恢复备份文件...")
            for f in backup_files:
                src = Path(backup_dir) / f.name
                if src.exists():
                    shutil.copy2(src, work_path)
            shutil.rmtree(backup_dir)
        
        # 如果同一天有多幅图像，执行reframe合并
        if len(self.scenes) > len(unique_dates):
            self.logger.info(f"检测到多幅场景，执行compute_reframe合并...")
            self.sbas.compute_reframe()
            self.logger.info(f"✓ 合并完成，场景数: {self.sbas.df.shape[0]}")
        
        return self
    
    def download_dem(self, lon_min: float, lat_min: float, lon_max: float, lat_max: float) -> str:
        """
        下载DEM数据使用pygmtsar内置方法
        通过Stack实例下载DEM，确保格式兼容
        
        Args:
            lon_min, lat_min, lon_max, lat_max: 区域边界
            
        Returns:
            DEM文件路径
        """
        import xarray as xr
        
        self.logger.info("="*70)
        self.logger.info("下载DEM数据...")
        self.logger.info(f"区域: [{lon_min}, {lat_min}, {lon_max}, {lat_max}]")
        self.logger.info("="*70)
        
        dem_path = Path(self.config.data_dir) / "DEM.nc"
        
        try:
            # 方法1: 使用Stack的get_dem方法（如果sbas已初始化）
            if hasattr(self, 'sbas') and self.sbas is not None:
                self.logger.info("使用Stack.get_dem()下载DEM...")
                dem = self.sbas.get_dem()
                dem.to_netcdf(str(dem_path), engine='netcdf4')
                self.logger.info(f"✓ DEM下载完成: {dem_path}")
                return str(dem_path)
        except Exception as e:
            self.logger.warning(f"Stack.get_dem()失败: {e}")
        
        # 方法2: 使用Tiles下载，然后修复格式
        try:
            self.logger.info("使用Tiles.download_dem()下载DEM...")
            aoi = gpd.GeoDataFrame(
                {"geometry": [shapely.geometry.box(lon_min, lat_min, lon_max, lat_max)]},
                crs="EPSG:4326"
            )
            tiles = Tiles()
            tiles.download_dem(
                geometry=aoi,
                filename=str(dem_path),
                product="1s",
                provider="GLO",
                n_jobs=4
            )
            self.logger.info(f"✓ DEM下载完成: {dem_path}")
            return str(dem_path)
        except Exception as e:
            self.logger.error(f"Tiles下载失败: {e}")
            raise
    
    def fix_dem_format(self, dem_path: str) -> str:
        """
        修复DEM格式以兼容pygmtsar
        Tiles.download_dem()生成的是Dataset格式，需要转换为DataArray
        
        Args:
            dem_path: 原始DEM文件路径
            
        Returns:
            修复后的DEM文件路径
        """
        import xarray as xr
        
        self.logger.info("检查DEM格式...")
        dem_file = Path(dem_path)
        
        # 检查文件是否存在
        if not dem_file.exists():
            # 尝试大小写变体
            alt_path = dem_file.parent / 'DEM.nc' if dem_file.name == 'dem.nc' else dem_file.parent / 'dem.nc'
            if alt_path.exists():
                dem_file = alt_path
            else:
                raise FileNotFoundError(f"DEM文件不存在: {dem_path}")
        
        # 尝试多种引擎打开文件
        engines_to_try = ['netcdf4', 'h5netcdf', None]
        ds = None
        
        for engine in engines_to_try:
            try:
                if engine:
                    ds = xr.open_dataset(str(dem_file), engine=engine)
                else:
                    ds = xr.open_dataset(str(dem_file))
                self.logger.info(f"  使用{engine or '默认'}引擎打开成功")
                break
            except Exception as e:
                self.logger.debug(f"  {engine or '默认'}引擎失败: {e}")
                continue
        
        if ds is None:
            raise RuntimeError(f"无法打开DEM文件: {dem_file}")
        
        try:
            # 提取变量（通常是'z'或'DEM'）
            if 'z' in ds.data_vars:
                dem_da = ds['z']
            elif 'DEM' in ds.data_vars:
                dem_da = ds['DEM']
            else:
                # 使用第一个数据变量
                dem_da = list(ds.data_vars.values())[0]
            
            # 设置变量名
            dem_da.name = "DEM"
            
            # 创建临时文件
            temp_path = dem_file.parent / "DEM_fixed.nc"
            
            # 保存为DataArray格式（使用netcdf4引擎更稳定）
            dem_da.to_netcdf(str(temp_path), engine='netcdf4')
            ds.close()
            
            # 备份原文件
            backup_path = dem_file.parent / f"{dem_file.name}.backup_tiles"
            if backup_path.exists():
                backup_path.unlink()
            shutil.move(str(dem_file), str(backup_path))
            self.logger.info(f"  原文件备份: {backup_path}")
            
            # 移动修复后的文件
            shutil.move(str(temp_path), str(dem_file))
            self.logger.info(f"✓ DEM格式修复完成: {dem_file}")
            
            return str(dem_file)
        except Exception as e:
            if ds is not None:
                ds.close()
            self.logger.error(f"修复DEM格式时出错: {e}")
            raise
    
    def ensure_dem_ready(self, lon_min: float = None, lat_min: float = None, 
                         lon_max: float = None, lat_max: float = None) -> str:
        """
        确保DEM数据就绪（下载+格式修复）
        
        Args:
            lon_min, lat_min, lon_max, lat_max: 可选的区域边界，用于下载DEM
            
        Returns:
            DEM文件路径
        """
        import xarray as xr
        
        dem_path = Path(self.config.dem_path)
        data_dir = Path(self.config.data_dir)
        
        # 检查DEM是否已存在且格式正确
        for dem_file in [dem_path, data_dir / 'DEM.nc', data_dir / 'dem.nc']:
            if dem_file.exists():
                try:
                    # 尝试打开验证格式
                    test_da = xr.open_dataarray(str(dem_file))
                    test_da.close()
                    self.logger.info(f"✓ DEM文件已就绪: {dem_file}")
                    # 更新配置
                    if dem_file.name != self.config.dem_file:
                        self.config.dem_file = dem_file.name
                    return str(dem_file)
                except (ValueError, TypeError):
                    # 需要修复格式
                    return self.fix_dem_format(str(dem_file))
        
        # DEM不存在，需要下载
        if lon_min is None:
            # 从配置或场景推断区域
            if hasattr(self, 'scenes') and self.scenes is not None and len(self.scenes) > 0:
                lon_min = float(self.scenes.geometry.bounds.minx.min()) - 0.1
                lon_max = float(self.scenes.geometry.bounds.maxx.max()) + 0.1
                lat_min = float(self.scenes.geometry.bounds.miny.min()) - 0.1
                lat_max = float(self.scenes.geometry.bounds.maxy.max()) + 0.1
            else:
                # 使用默认重庆区域
                lon_min, lat_min, lon_max, lat_max = 106.2, 29.2, 107.0, 29.9
        
        # 下载DEM
        downloaded_path = self.download_dem(lon_min, lat_min, lon_max, lat_max)
        
        # 修复格式
        return self.fix_dem_format(downloaded_path)
    
    def step_01_scene_locations(self) -> 'InSARProcessor':
        """步骤01: 场景位置估计 - 参考 notebook"""
        self.logger.info("步骤01: 绘制场景位置...")
        
        # 参考 notebook：sbas.plot_scenes(POI=POI)
        fig = self.sbas.plot_scenes(POI=self.poi)
        plt.title('Estimated Scene Locations')
        plt.xlabel('lon')
        plt.ylabel('lat')
        
        filename = self._get_output_filename("Estimated Scene Locations", 1)
        filepath = Path(self.config.results_dir) / filename
        plt.savefig(filepath, dpi=150, bbox_inches='tight')
        plt.close()
        
        self.logger.info(f"✓ {filename}")
        return self
    
    def step_02_scene_locations_with_dem(self) -> 'InSARProcessor':
        """步骤02: 场景位置与DEM"""
        self.logger.info("步骤02: 加载DEM并绘制场景位置...")
        
        # 确保DEM就绪（下载+格式修复）
        dem_path = self.ensure_dem_ready()
        
        # 如果DEM路径与配置不同，更新Stack的DEM路径
        if dem_path != self.config.dem_path:
            self.logger.info(f"使用修复后的DEM: {dem_path}")
        
        # 使用sbas.load_dem加载DEM（monkey patch已替换引擎为netcdf4）
        # 参考 notebook：sbas.load_dem(DEM, AOI)
        self.sbas.load_dem(dem_path)
        
        # 参考 notebook：绘制场景位置并保存
        fig = self.sbas.plot_scenes(POI=self.poi)
        
        filename = self._get_output_filename("Estimated Scene Locations", 2)
        filepath = Path(self.config.results_dir) / filename
        plt.savefig(filepath, dpi=150, bbox_inches='tight')
        plt.close()
        
        self.logger.info(f"✓ {filename}")
        return self
    
    def step_03_align(self) -> 'InSARProcessor':
        """步骤03: 图像配准"""
        self.logger.info("步骤03: 图像配准...")
        self.sbas.compute_align()
        self.logger.info("✓ 图像配准完成")
        return self
    
    def step_04_geocode(self) -> 'InSARProcessor':
        """步骤04: 地理编码"""
        self.logger.info("步骤04: 地理编码...")
        # 使用更小的分辨率（更粗的网格）以确保ra2ll转换成功
        # resolution应该是像素大小（米），较大的值产生较粗的网格
        geocode_resolution = max(self.config.resolution, 120)  # 至少120米
        self.sbas.compute_geocode(geocode_resolution)
        self.logger.info(f"✓ 地理编码完成 (分辨率: {geocode_resolution}m)")
        return self
    
    def step_05_topography(self) -> 'InSARProcessor':
        """步骤05: WGS84椭球面地形"""
        self.logger.info("步骤05: 绘制地形图...")
        
        try:
            # 参考 notebook：sbas.plot_topo(POI=sbas.geocode(POI))
            fig = self.sbas.plot_topo(POI=self.sbas.geocode(self.poi))
            plt.title('Topography on WGS84 ellipsoid, [m]')
            plt.xlabel('Range')
            plt.ylabel('Azimuth')
            
            filename = self._get_output_filename("Topography on WGS84 ellipsoid", 3)
            filepath = Path(self.config.results_dir) / filename
            plt.savefig(filepath, dpi=150, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"✓ {filename}")
        except Exception as e:
            self.logger.warning(f"地形图绘制失败: {e}")
        
        return self
    
    def step_06_interferogram(self) -> 'InSARProcessor':
        """步骤06: 生成干涉图"""
        self.logger.info("步骤06: 生成干涉图...")
        
        # 参考 notebook：pairs = [sbas.to_dataframe().index.unique()]
        pairs = [self.sbas.to_dataframe().index.unique()]
        self.topo = self.sbas.get_topo()
        self.data = self.sbas.open_data()
        
        # 多视强度 - 参考 notebook
        self.logger.info("  计算多视强度...")
        intensity_mlook = self.sbas.multilooking(
            np.square(np.abs(self.data)),
            wavelength=self.config.multilook_wavelength,
            coarsen=self.config.multilook_coarsen
        )
        
        # 相位差 - 参考 notebook
        self.logger.info("  计算相位差...")
        phase = self.sbas.phasediff(pairs, self.data, self.topo)
        
        # 多视相位 - 参考 notebook
        self.logger.info("  多视处理相位...")
        phase_mlook = self.sbas.multilooking(
            phase,
            wavelength=self.config.multilook_wavelength,
            coarsen=self.config.multilook_coarsen
        )
        
        # 相干性 - 参考 notebook
        self.logger.info("  计算相干性...")
        corr_mlook = self.sbas.correlation(phase_mlook, intensity_mlook)
        
        # Goldstein滤波 - 参考 notebook
        self.logger.info("  Goldstein滤波...")
        phase_mlook_goldstein = self.sbas.goldstein(
            phase_mlook, 
            corr_mlook, 
            self.config.goldstein_alpha
        )
        
        # 创建干涉图 - 参考 notebook
        self.logger.info("  创建干涉图...")
        intf_mlook = self.sbas.interferogram(phase_mlook_goldstein)
        
        # 持久化结果 - 参考 notebook
        self.logger.info("  持久化结果...")
        self.corr, self.intf = dask.persist(corr_mlook[0], intf_mlook[0])
        tqdm_dask((self.corr, self.intf), desc='计算相位和相干性')
        
        self.logger.info("✓ 干涉图生成完成")
        return self
    
    def step_07_geocode_conversion(self) -> 'InSARProcessor':
        """步骤07: 转换为地理坐标"""
        self.logger.info("步骤07: 转换到地理坐标...")
        
        # 参考 notebook：直接进行地理编码转换
        try:
            # 地理编码到地理坐标并裁剪空白边界
            self.intf_ll = self.sbas.ra2ll(self.intf)
            self.corr_ll = self.sbas.ra2ll(self.corr)
            self._use_geographic = True
            self.logger.info("✓ 地理编码转换完成")
        except Exception as e:
            self.logger.warning(f"地理编码转换失败: {e}")
            self.logger.warning("将使用雷达坐标系继续处理...")
            # 使用雷达坐标系数据
            self.intf_ll = self.intf
            self.corr_ll = self.corr
            self._use_geographic = False
        
        return self
    
    def step_08_phase_plot(self) -> 'InSARProcessor':
        """步骤08: 干涉相位图"""
        self.logger.info("步骤08: 绘制干涉相位...")
        
        # 参考 notebook：sbas.plot_interferogram(intf_ll, caption='Phase\nGeographic Coordinates, [rad]', POI=POI)
        caption = 'Phase\nGeographic Coordinates, [rad]' if self._use_geographic else 'Phase\nRadar Coordinates, [rad]'
        fig = self.sbas.plot_interferogram(
            self.intf_ll,
            caption=caption,
            POI=self.poi if self._use_geographic else None
        )
        plt.xlabel('lon')
        plt.ylabel('lat')
        
        filename = self._get_output_filename("Phase Geographic Coordinates", 4)
        filepath = Path(self.config.results_dir) / filename
        plt.savefig(filepath, dpi=150, bbox_inches='tight')
        plt.close()
        
        self.logger.info(f"✓ {filename}")
        return self
    
    def step_09_correlation_plot(self) -> 'InSARProcessor':
        """步骤09: 相干系数图"""
        self.logger.info("步骤09: 绘制相干系数...")
        
        # 参考 notebook：sbas.plot_correlation(corr_ll, caption='Correlation\nGeographic Coordinates', POI=POI)
        caption = 'Correlation\nGeographic Coordinates' if self._use_geographic else 'Correlation\nRadar Coordinates'
        fig = self.sbas.plot_correlation(
            self.corr_ll,
            caption=caption,
            POI=self.poi if self._use_geographic else None
        )
        plt.xlabel('lon')
        plt.ylabel('lat')
        
        filename = self._get_output_filename("Correlation Geographic Coordinates", 5)
        filepath = Path(self.config.results_dir) / filename
        plt.savefig(filepath, dpi=150, bbox_inches='tight')
        plt.close()
        
        self.logger.info(f"✓ {filename}")
        return self
    
    def _load_landmask(self):
        """加载陆地掩膜 - 支持地理和雷达坐标系"""
        self.logger.info("加载陆地掩膜...")
        
        # 参考 notebook：sbas.load_landmask(LANDMASK)
        self.sbas.load_landmask(self.config.landmask_path)
        
        # 参考 notebook：
        # landmask_ll = sbas.get_landmask().reindex_like(intf_ll, method='nearest')
        # landmask = sbas.ll2ra(landmask_ll).reindex_like(intf, method='nearest')
        landmask_orig = self.sbas.get_landmask()
        self.logger.info(f"  原始landmask维度: {landmask_orig.shape}, dims: {landmask_orig.dims}")
        
        # 重采样到与地理坐标干涉图匹配
        self.landmask_ll = landmask_orig.reindex_like(self.intf_ll, method='nearest')
        self.logger.info(f"  地理坐标landmask维度: {self.landmask_ll.shape}")
        
        # 转换到雷达坐标系并重采样
        try:
            self.logger.info("  转换landmask到雷达坐标系...")
            landmask_ra = self.sbas.ll2ra(self.landmask_ll)
            self.landmask = landmask_ra.reindex_like(self.intf, method='nearest')
            self.logger.info(f"  雷达坐标landmask维度: {self.landmask.shape}")
        except Exception as e:
            self.logger.warning(f"ll2ra转换失败: {e}，创建默认landmask")
            import numpy as np
            import xarray as xr
            self.landmask = xr.DataArray(
                np.ones_like(self.intf.values, dtype=bool),
                dims=self.intf.dims,
                coords=self.intf.coords
            )
    
    def step_10_landmask_plot(self) -> 'InSARProcessor':
        """步骤10: 陆地掩膜"""
        self.logger.info("步骤10: 绘制陆地掩膜...")
        self._load_landmask()
        
        # 参考 notebook：sbas.plot_landmask(landmask=landmask_ll, caption='Landmask\nGeographic Coordinates', POI=POI)
        caption = 'Landmask\nGeographic Coordinates' if self._use_geographic else 'Landmask\nRadar Coordinates'
        fig = self.sbas.plot_landmask(
            landmask=self.landmask_ll,
            caption=caption,
            POI=self.poi if self._use_geographic else None
        )
        plt.xlabel('lon')
        plt.ylabel('lat')
        
        filename = self._get_output_filename("Landmask Geographic Coordinates", 6)
        filepath = Path(self.config.results_dir) / filename
        plt.savefig(filepath, dpi=150, bbox_inches='tight')
        plt.close()
        
        self.logger.info(f"✓ {filename}")
        return self
    
    def step_11_landmasked_phase(self) -> 'InSARProcessor':
        """步骤11: 掩膜后干涉相位"""
        self.logger.info("步骤11: 绘制掩膜后相位...")
        
        try:
            # 参考 notebook：sbas.plot_interferogram(intf_ll.where(landmask_ll), caption='Landmasked Phase\nGeographic Coordinates, [rad]', POI=POI)
            caption = 'Landmasked Phase\nGeographic Coordinates, [rad]' if self._use_geographic else 'Landmasked Phase\nRadar Coordinates, [rad]'
            
            if self._use_geographic:
                intf_masked = self.intf_ll.where(self.landmask_ll)
            else:
                intf_masked = self.intf.where(self.landmask)
            
            fig = self.sbas.plot_interferogram(
                intf_masked,
                caption=caption,
                POI=self.poi if self._use_geographic else None
            )
            plt.xlabel('lon')
            plt.ylabel('lat')
            
            filename = self._get_output_filename("Landmasked Phase Geographic Coordinates", 7)
            filepath = Path(self.config.results_dir) / filename
            plt.savefig(filepath, dpi=150, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"✓ {filename}")
        except Exception as e:
            self.logger.warning(f"掩膜后相位绘制失败: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
        
        return self
    
    def step_12_phase_unwrapping(self) -> 'InSARProcessor':
        """步骤12: 相位解缠"""
        self.logger.info("步骤12: 相位解缠(SNAPHU)...")
        
        try:
            # 参考 notebook：
            # conf = sbas.snaphu_config(defomax=None, NTILEROW=4, NTILECOL=4, ROWOVRLP=200, COLOVRLP=200)
            # tqdm_dask(unwrap := sbas.unwrap_snaphu(intf.where(landmask), corr, conf=conf).persist(), desc='SNAPHU Unwrapping')
            conf = self.sbas.snaphu_config(
                defomax=None,
                NTILEROW=self.config.snaphu_tiles_row,
                NTILECOL=self.config.snaphu_tiles_col,
                ROWOVRLP=self.config.snaphu_overlap,
                COLOVRLP=self.config.snaphu_overlap
            )
            self.logger.info(f"SNAPHU配置: {conf}")
            
            # SNAPHU需要雷达坐标系的数据
            intf_ra = self.intf
            corr_ra = self.corr
            landmask_ra = self.landmask
            
            # 应用掩膜并进行解缠 - 参考 notebook
            phase_masked = intf_ra.where(landmask_ra)
            
            self.unwrap = self.sbas.unwrap_snaphu(
                phase_masked,
                corr_ra,
                conf=conf
            ).persist()
            
            tqdm_dask(self.unwrap, desc='SNAPHU Unwrapping')
            self.logger.info("✓ 相位解缠完成")
        except Exception as e:
            self.logger.error(f"相位解缠失败: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
            self.unwrap = None
        
        return self
    
    def step_13_unwrapped_phase(self) -> 'InSARProcessor':
        """步骤13: 解缠相位图"""
        if self.unwrap is None:
            self.logger.warning("步骤13: 跳过解缠相位图（解缠失败）")
            return self
            
        self.logger.info("步骤13: 绘制解缠相位...")
        
        try:
            # 参考 notebook：
            # unwrap_ll = sbas.ra2ll(unwrap.phase)
            # sbas.plot_phase(unwrap_ll, caption='Unwrapped Phase\nGeographic Coordinates, [rad]', quantile=[0.01, 0.99], POI=POI)
            if self._use_geographic:
                unwrap_ll = self.sbas.ra2ll(self.unwrap.phase)
            else:
                unwrap_ll = self.unwrap.phase
                
            caption = 'Unwrapped Phase\nGeographic Coordinates, [rad]' if self._use_geographic else 'Unwrapped Phase\nRadar Coordinates, [rad]'
            fig = self.sbas.plot_phase(
                unwrap_ll,
                caption=caption,
                quantile=[0.01, 0.99],
                POI=self.poi if self._use_geographic else None
            )
            plt.xlabel('lon')
            plt.ylabel('lat')
            
            filename = self._get_output_filename("Unwrapped Phase Geographic Coordinates", 8)
            filepath = Path(self.config.results_dir) / filename
            plt.savefig(filepath, dpi=150, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"✓ {filename}")
        except Exception as e:
            self.logger.warning(f"解缠相位图绘制失败: {e}")
        
        return self
    
    def step_14_detrend(self) -> 'InSARProcessor':
        """步骤14: 去趋势"""
        if self.unwrap is None:
            self.logger.warning("步骤14: 跳过去趋势（解缠失败）")
            return self
            
        self.logger.info(f"步骤14: 高斯滤波去趋势({self.config.detrend_wavelength/1000:.0f}km)...")
        
        try:
            # 参考 notebook：
            # tqdm_dask(detrend := (unwrap.phase - sbas.gaussian(unwrap.phase, wavelength=300000)).persist(), desc='Detrending')
            self.detrend = (
                self.unwrap.phase - 
                self.sbas.gaussian(
                    self.unwrap.phase,
                    wavelength=self.config.detrend_wavelength
                )
            ).persist()
            
            tqdm_dask(self.detrend, desc='Detrending')
            self.logger.info("✓ 去趋势完成")
        except Exception as e:
            self.logger.warning(f"去趋势失败: {e}")
            self.detrend = None
        
        return self
    
    def step_15_detrended_phase(self) -> 'InSARProcessor':
        """步骤15: 去趋势解缠相位"""
        if self.detrend is None:
            self.logger.warning("步骤15: 跳过去趋势相位图（去趋势失败）")
            return self
            
        self.logger.info("步骤15: 绘制去趋势后相位...")
        
        try:
            # 参考 notebook：
            # detrend_ll = sbas.ra2ll(detrend)
            # sbas.plot_phase(detrend_ll, caption='Detrended Unwrapped Phase\nGeographic Coordinates, [rad]', quantile=[0.01, 0.99], POI=POI)
            if self._use_geographic:
                detrend_ll = self.sbas.ra2ll(self.detrend)
            else:
                detrend_ll = self.detrend
                
            caption = 'Detrended Unwrapped Phase\nGeographic Coordinates, [rad]' if self._use_geographic else 'Detrended Unwrapped Phase\nRadar Coordinates, [rad]'
            fig = self.sbas.plot_phase(
                detrend_ll,
                caption=caption,
                quantile=[0.01, 0.99],
                POI=self.poi if self._use_geographic else None
            )
            plt.xlabel('lon')
            plt.ylabel('lat')
            
            filename = self._get_output_filename("Detrended Unwrapped Phase Geographic Coordinates", 9)
            filepath = Path(self.config.results_dir) / filename
            plt.savefig(filepath, dpi=150, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"✓ {filename}")
        except Exception as e:
            self.logger.warning(f"去趋势相位图绘制失败: {e}")
        
        return self
    
    def step_16_los_displacement(self) -> 'InSARProcessor':
        """步骤16: LOS向位移"""
        if self.detrend is None:
            self.logger.warning("步骤16: 跳过LOS位移（去趋势失败）")
            return self
            
        self.logger.info("步骤16: 计算并绘制LOS向位移...")
        
        try:
            # 参考 notebook：
            # los_disp_mm_ll = sbas.ra2ll(sbas.los_displacement_mm(detrend))
            # sbas.plot_displacement(los_disp_mm_ll, caption='Detrended LOS Displacement\nGeographic Coordinates, [mm]', quantile=[0.01, 0.99], POI=POI)
            los_disp_mm = self.sbas.los_displacement_mm(self.detrend)
            if self._use_geographic:
                los_disp_mm = self.sbas.ra2ll(los_disp_mm)
            
            caption = 'Detrended LOS Displacement\nGeographic Coordinates, [mm]' if self._use_geographic else 'LOS Displacement\nRadar Coordinates, [mm]'
            fig = self.sbas.plot_displacement(
                los_disp_mm,
                caption=caption,
                quantile=[0.01, 0.99],
                POI=self.poi if self._use_geographic else None
            )
            plt.xlabel('lon')
            plt.ylabel('lat')
            
            filename = self._get_output_filename("Detrended LOS Displacement Geographic Coordinates", 10)
            filepath = Path(self.config.results_dir) / filename
            plt.savefig(filepath, dpi=150, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"✓ {filename}")
        except Exception as e:
            self.logger.warning(f"LOS位移计算失败: {e}")
        
        return self
    
    def step_17_incidence_angle(self) -> 'InSARProcessor':
        """步骤17: 雷达入射角"""
        self.logger.info("步骤17: 绘制雷达入射角...")
        
        try:
            # 参考 notebook：sbas.plot_incidence_angle(POI=sbas.geocode(POI))
            fig = self.sbas.plot_incidence_angle(POI=self.sbas.geocode(self.poi))
            plt.xlabel('lon')
            plt.ylabel('lat')
            
            filename = self._get_output_filename("Incidence Angle Geographic Coordinates", 11)
            filepath = Path(self.config.results_dir) / filename
            plt.savefig(filepath, dpi=150, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"✓ {filename}")
        except Exception as e:
            self.logger.warning(f"入射角绘制失败: {e}")
        
        return self
    
    def step_18_vertical_displacement(self) -> 'InSARProcessor':
        """步骤18: 垂直方向位移 - 参考用户提供的图片"""
        if self.detrend is None:
            self.logger.warning("步骤18: 跳过垂直位移（去趋势失败）")
            return self
            
        self.logger.info("步骤18: 计算垂直方向投影...")
        
        try:
            # 参考 notebook：
            # vert_disp_mm_ll = sbas.ra2ll(sbas.vertical_displacement_mm(detrend))
            # sbas.plot_displacement(vert_disp_mm_ll, caption='Vertical Projection LOS Displacement\nGeographic Coordinates, [mm]', quantile=[0.01, 0.99], POI=POI)
            vert_disp_mm = self.sbas.vertical_displacement_mm(self.detrend)
            if self._use_geographic:
                vert_disp_mm = self.sbas.ra2ll(vert_disp_mm)
            
            caption_vert = 'Vertical Projection LOS Displacement\nGeographic Coordinates, [mm]' if self._use_geographic else 'Vertical Projection\nRadar Coordinates, [mm]'
            fig = self.sbas.plot_displacement(
                vert_disp_mm,
                caption=caption_vert,
                quantile=[0.01, 0.99],
                POI=self.poi if self._use_geographic else None
            )
            plt.xlabel('lon')
            plt.ylabel('lat')
            
            filename = self._get_output_filename("Vertical Projection LOS Displacement Geographic Coordinates", 12)
            filepath = Path(self.config.results_dir) / filename
            plt.savefig(filepath, dpi=150, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"✓ {filename}")
        except Exception as e:
            self.logger.warning(f"垂直位移计算失败: {e}")
        
        return self
    
    def step_19_eastwest_displacement(self) -> 'InSARProcessor':
        """步骤19: 东西方向位移"""
        if self.detrend is None:
            self.logger.warning("步骤19: 跳过东西向位移（去趋势失败）")
            return self
            
        self.logger.info("步骤19: 计算东西方向投影...")
        
        try:
            # 参考 notebook：
            # east_disp_mm_ll = sbas.ra2ll(sbas.eastwest_displacement_mm(detrend))
            # sbas.plot_displacement(east_disp_mm_ll, caption='East-West Projection LOS Displacement\nGeographic Coordinates, [mm]', quantile=[0.01, 0.99], POI=POI)
            east_disp_mm = self.sbas.eastwest_displacement_mm(self.detrend)
            if self._use_geographic:
                east_disp_mm = self.sbas.ra2ll(east_disp_mm)
            
            caption_ew = 'East-West Projection LOS Displacement\nGeographic Coordinates, [mm]' if self._use_geographic else 'East-West Projection\nRadar Coordinates, [mm]'
            fig = self.sbas.plot_displacement(
                east_disp_mm,
                caption=caption_ew,
                quantile=[0.01, 0.99],
                POI=self.poi if self._use_geographic else None
            )
            plt.xlabel('lon')
            plt.ylabel('lat')
            
            filename = self._get_output_filename("East-West Projection LOS Displacement Geographic Coordinates", 13)
            filepath = Path(self.config.results_dir) / filename
            plt.savefig(filepath, dpi=150, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"✓ {filename}")
        except Exception as e:
            self.logger.warning(f"东西向位移计算失败: {e}")
        
        return self
    
    def run_full_pipeline(self) -> None:
        """运行完整处理流程 (模板方法模式)"""
        try:
            self.logger.info("="*70)
            self.logger.info("开始InSAR完整处理流程")
            self.logger.info("="*70)
            
            # 初始化
            self.initialize_dask()
            self.load_data()
            
            # 执行各步骤 (流畅接口模式) - 参考 notebook 流程
            (self
                .step_01_scene_locations()
                .step_02_scene_locations_with_dem()
                .step_03_align()
                .step_04_geocode()
                .step_05_topography()
                .step_06_interferogram()
                .step_07_geocode_conversion()
                .step_08_phase_plot()
                .step_09_correlation_plot()
                .step_10_landmask_plot()
                .step_11_landmasked_phase()
                .step_12_phase_unwrapping()
                .step_13_unwrapped_phase()
                .step_14_detrend()
                .step_15_detrended_phase()
                .step_16_los_displacement()
                .step_17_incidence_angle()
                .step_18_vertical_displacement()
                .step_19_eastwest_displacement()
            )
            
            self.logger.info("="*70)
            self.logger.info("所有图像生成成功!")
            self.logger.info("="*70)
            self.logger.info(f"结果保存至: {Path(self.config.results_dir).absolute()}")
            
        except Exception as e:
            self.logger.error(f"处理过程中发生错误: {e}", exc_info=True)
            raise
        finally:
            if self.client:
                self.client.close()
                self.logger.info("Dask客户端已关闭")
    
    def run_steps(self, step_indices: List[int]) -> None:
        """
        运行指定步骤 (策略模式)
        
        Args:
            step_indices: 步骤索引列表，如 [1, 2, 3] 或 [8, 9, 10]
        """
        steps = {
            1: self.step_01_scene_locations,
            2: self.step_02_scene_locations_with_dem,
            3: self.step_03_align,
            4: self.step_04_geocode,
            5: self.step_05_topography,
            6: self.step_06_interferogram,
            7: self.step_07_geocode_conversion,
            8: self.step_08_phase_plot,
            9: self.step_09_correlation_plot,
            10: self.step_10_landmask_plot,
            11: self.step_11_landmasked_phase,
            12: self.step_12_phase_unwrapping,
            13: self.step_13_unwrapped_phase,
            14: self.step_14_detrend,
            15: self.step_15_detrended_phase,
            16: self.step_16_los_displacement,
            17: self.step_17_incidence_angle,
            18: self.step_18_vertical_displacement,
            19: self.step_19_eastwest_displacement,
        }
        
        self.initialize_dask()
        self.load_data()
        
        for idx in step_indices:
            if idx in steps:
                self.logger.info(f"执行步骤 {idx}...")
                steps[idx]()
            else:
                self.logger.warning(f"步骤 {idx} 不存在")
        
        if self.client:
            self.client.close()


def main():
    """主函数 - 演示如何使用InSARProcessor类处理土耳其地震数据"""
    # 创建配置对象 - 参考 Türkiye_Earthquakes_2023.ipynb
    config = InSARConfig(
        work_dir='raw_kahr',
        data_dir='data_proc',
        results_dir='results/turkey',  # 保存到 turkey 子目录
        dem_file='dem.nc',
        landmask_file='landmask.nc',
        polarization='VV',
        resolution=180.0,
        earthquake_locations=[
            ('M7.8', 37.217, 37.032),  # M7.8 地震位置 (lon, lat)
            ('M7.5', 37.028, 37.206),  # M7.5 地震位置 (lon, lat)
        ]
    )
    
    # 创建处理器并运行
    processor = InSARProcessor(config)
    processor.run_full_pipeline()


if __name__ == '__main__':
    main()
