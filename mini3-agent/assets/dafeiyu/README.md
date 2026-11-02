# Mini3 大肥鱼素材

作者：YunYueSama  
仓库：https://github.com/YunYueSama/codex-deepseek-pet  
来源版本：`7661c8b304c5400701f91da01b1a643a207331de`

本项目修改：从 `assets/whale/idle-front.png`、`expressions.png` 和 `dense-sleep.png` 选取14个姿势，统一参考框、缩放，去掉上下空白，按现有深色界面合成底色，转换为176×144 RGB565小端序常量帧，适配Arcs-Mini3的240×240显示。后增加打瞌睡、坐下、蜷卧帧以衔接入睡和醒来。没有运行上游Electron程序，没有使用社区灵感原图。待机含眨眼和轻微呼吸位移；说话在两个关键姿态间切换，并非逐音素口型。像素数据共709,632字节。

完整许可及上游权利说明保存在 `upstream/LICENSE`、`upstream/ASSET_LICENSE.md`。许可覆盖上游作者有权授权的贡献，不代表 DeepSeek 官方授权。已注明这些素材经过修改；不将其描述为本项目原创角色。

转换脚本：`../../tools/build_dafeiyu_assets.py`，工具依赖在 `../../tools/requirements-assets.txt`。`manifest.json` 记录源文件 SHA256、来源提交、帧名称及总像素字节数。`mini3-preview.png` 和 `preview.html` 是电脑布局预览，不是实机截图；示例电量/天气只用于预览。设备端仍读取真实电量和网络状态。

输出到固件的 `pet_whale_assets.c/.h` 编入 CP 应用，不改变 Mini3 分区表；原始素材和电脑预览不会被烧入设备。
