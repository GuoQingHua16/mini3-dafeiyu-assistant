# Mini3 固件接入

本仓库不分发整套官方SDK或预编译固件。提供的是固定官方源码基线上的应用层补丁，以及大肥鱼界面、本地控制服务、MP3完整帧检查等新增文件。

- 官方来源：`https://cloud.listenai.com/CSKG836746/arcs-sdk/public/arcs_mini.git`
- 基线提交：`620341ff97b7c5119b1c3e5796b3bc4e0dca3505`
- 板型：`arcs_mini3`
- 分区表：`res/arcs-mini3/partition_table.json`
- 本项目部署范围：CP app

## 准备与应用补丁

在本仓库根目录执行：

```powershell
git clone https://cloud.listenai.com/CSKG836746/arcs-sdk/public/arcs_mini.git mini3-firmware
git -C mini3-firmware checkout 620341ff97b7c5119b1c3e5796b3bc4e0dca3505
python .\tools\apply_firmware.py --sdk .\mini3-firmware --check
python .\tools\apply_firmware.py --sdk .\mini3-firmware
```

按官方工程说明准备编译工具链及依赖。补丁工具会验证目标提交和相关文件的内容，清空源码中的默认云端产品ID/密钥值，再应用修改和新增文件；不会替你登录平台、修改设备绑定或烧录。已有自定义修改的目标文件会被拒绝，避免覆盖。

固件使用 `CONFIG_MINI3_LOCAL_AGENT=y` 拒绝创建聆思云客户端，关闭OTA，使用电脑端进行识别与对话。

## 构建与写入

在官方工程目录运行：

```powershell
.\build.ps1 -S .\apps\arcs-mini -B .\build-local -DBOARD=arcs_mini3
```

如果工具链不能处理路径中的中文或空格，可按官方说明把工程映射到空闲盘符后构建。

确认连接的是自己的Mini3、固件构建成功、分区表正确后，使用官方脚本的app模式：

```powershell
.\adb_download.ps1 -S .\res\arcs-mini3 -B .\build-local -Mode app
```

不要混用其它板型的镜像或分区表。此补丁没有要求更新AP或Boot。烧录完成后，应先检查 `local_agent info` 中的 `HUD=1 AUDIO_BUFFER=1 POLL=1`，再验证USB采音、局域网下载与实际播放。

## 恢复入口的项目记录

本项目这块Mini3曾通过“按住主功能键，短按并松开RST，再松开功能键”进入Boot Recovery。是否进入成功，要以电脑实际出现可通信的Recovery设备为准；`offline` 不能当作可刷状态。Boot版本和恢复流程应对照官方说明，不凭按键动作本身推断成功。

该次恢复保留了旧版交互Shell连接，随后按Mini3分区表只写回稳定CP。它是本机故障恢复记录，不是所有Boot版本都适用的通用强刷脚本。常规恢复优先使用官方工具；更深入的操作参考 [官方串口恢复说明](https://docs2.listenai.com/x/uaiwdDObz)。
