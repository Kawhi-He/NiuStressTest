# NiuStressTest

这是一个给小牛雷达 RCU 做压力测试的脚本集合。

## 你需要准备什么

1. 一台 Windows 电脑
2. 一部已经打开 USB 调试的安卓手机
3. 已安装 `NIU Ble Tools` 的手机
4. 一台程控电源
5. 一根 USB 数据线

## 先安装什么

### 1. 安装 Python

先安装 Python 3.10+。

安装后在命令行里执行：

```powershell
python --version
pip --version
```

如果能看到版本号，说明 Python 可用。

### 2. 安装 Python 依赖

在这个目录下执行：

```powershell
pip install pyserial
```

这个脚本主要只需要 `pyserial`。

### 3. 打开手机 USB 调试

手机需要：

1. 打开开发者模式
2. 打开 USB 调试
3. 用数据线连到电脑
4. 手机弹框时点允许调试

然后执行：

```powershell
adb devices
```

如果能看到手机序列号和 `device`，说明连接正常。

## 这个仓库里有什么

- `radar_power_cycle_test.py`：主脚本，支持两个压力
- `niu_bletools.py`：控制 NIU Ble Tools 的页面操作
- `programmable_power.py`：控制程控电源

## 主脚本怎么用

脚本开头已经放了常用配置，小白一般只要改这里就够了。

### 重要配置

- `DEFAULT_STRESS_MODE`
  - `0`：两个压力都跑
  - `1`：只跑 OTA 杀后台压力
  - `2`：只跑断电上电读取雷达压力
- `DEFAULT_POWER_PORT`
  - 程控电源串口号，比如 `COM25`
- `DEFAULT_BAUDRATE`
  - 程控电源波特率，当前默认 `115200`
- `DEFAULT_ITERATIONS`
  - 断电上电读取压力的循环次数

### 压力 1

OTA 升级过程中，每到 1% 杀一次后台，再重新打开 APP 继续 OTA。

流程：

1. 打开 `NIU Ble Tools`
2. 进入 `蓝牙OTA升级`
3. 连接蓝牙
4. 选择 `外设-雷达`
5. 开始 OTA
6. 每到一个百分比就杀后台
7. 重新打开 APP 继续 OTA
8. OTA 成功后，断电重启
9. 上电后读取雷达三项数据

### 压力 2

每轮做一次完整断电上电，然后读取雷达三项数据。

流程：

1. 程控电源断电
2. 检查电压接近 0V
3. 程控电源 12V 上电
4. 检查电压接近 12V
5. 打开 `雷达RCU蓝牙指令验证`
6. 连接蓝牙
7. 读取三项数据

## 最常用的运行命令

### 只跑压力 2

```powershell
python .\radar_power_cycle_test.py --stress-mode 2
```

### 只跑压力 1

```powershell
python .\radar_power_cycle_test.py --stress-mode 1
```

### 两个压力都跑

```powershell
python .\radar_power_cycle_test.py --stress-mode 0
```

## 常见配置示例

```powershell
python .\radar_power_cycle_test.py --stress-mode 2 --iterations 100 --power-port COM25 --baudrate 115200 --voltage 12 --current 3
```

## 日志会写到哪里

脚本会自动生成 `logs/` 目录，里面有：

- `.log`：详细运行日志
- `.csv`：每轮结果表

日志里会包含：

- 时间戳
- 时区
- 日志等级
- 文件名和行号
- APP 下方日志，使用 `APP_LOG` 区分

## 如果报错，先看什么

### 1. `adb devices` 没有手机

先检查：

- 数据线
- USB 调试是否打开
- 手机是否点了允许调试

### 2. 程控电源读不到电压

先检查：

- 串口号是不是对的
- 波特率是不是 `115200`
- 电源有没有连上电脑

### 3. APP 读不到数据

先检查：

- 手机上是否真的打开了 `NIU Ble Tools`
- 是否已经连接蓝牙
- 是否选择了 `外设-雷达`

## 推荐你先这样跑

先跑一个小的测试：

```powershell
python .\radar_power_cycle_test.py --stress-mode 2 --iterations 1
```

确认没问题后，再改成更大的次数。
