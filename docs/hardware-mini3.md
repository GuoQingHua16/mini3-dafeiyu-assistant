# 本项目的 Arcs-Mini3 基线

本项目只针对 Arcs-Mini3，构建板型为 `arcs_mini3`，烧录使用官方工程的 `res/arcs-mini3/partition_table.json`。

| 项目 | 本项目使用的基线 |
| --- | --- |
| 主控 | LS2663、QFN76；按官方 Mini3 页面与规格型号表对应，未独立核对实物丝印 |
| CPU | 双 Nuclei N310 RISC-V，最高300MHz |
| SRAM | 总量704KB，包含系统与通信占用，不等于应用可用容量 |
| PSRAM | 8MB；内存规划还要计入固件、通信、音频和图像缓冲 |
| NPU | 本项目不具备NPU能力 |
| 屏幕 | 240×240 SPI LCD，ST7789P3 |
| 摄像头 | GC0328；本项目未实现视觉理解，实际采集规格另行验证 |
| 音频 | 板载麦克风及扬声器 |
| 网络 | 2.4GHz Wi-Fi；本项目不依赖未确认配备的4G模组 |
| USB | 使用设备模式下的 UAC 与 ADB |
| 扩展 MCU | CH32V003，经 I2C1 连接，地址0x6C |
| TF | SDIO，PA04–PA09用于该接口；不增加运行内存，当前功能不依赖TF卡 |

芯片接口上限不能直接当成板载外设性能。芯片/板卡完整参数以 [Mini3 官方文档](https://docs2.listenai.com/x/1KVekpl1e) 和实际板卡为准，接口存在与当前固件可用性需分别确认。
