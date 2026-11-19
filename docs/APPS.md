# 部署原生 App

Mac、iPhone 和 iPad 共用 `native/Sources/` 里的一套 SwiftUI 代码。目前没有发布到 App Store，也没有提供签好名的安装包，需要自己用 Xcode 构建。

`native/Jurvis.xcodeproj` 由 `scripts/generate_xcode.py` 生成。新增或删除源文件后重新运行它，不要手工改工程文件。

## Mac

```bash
./scripts/build-apps.sh
```

产物在 `build/release/Build/Products/Release/Jurvis.app`，使用本地的临时签名（ad-hoc），可以直接打开或拖进「应用程序」。

需要知道的限制：

- App 里记录了构建时的项目目录，引擎不在线时从那里启动它。**移动或重命名项目目录后要重新构建**。
- 它不是可以拷给别人的安装包：没有公证，也不自带引擎、运行时和模型。别人要用，需要在自己的 Mac 上克隆仓库并构建。

## iPhone / iPad 模拟器

`build-apps.sh` 同时构建模拟器版本，产物在 `build/mobile-release/Build/Products/Release-iphonesimulator/Jurvis.app`。

```bash
xcrun simctl install booted build/mobile-release/Build/Products/Release-iphonesimulator/Jurvis.app
```

```bash
xcrun simctl launch booted app.jurvis.mobile
```

## 装到自己的 iPhone / iPad

需要一个 Apple ID。免费账号可以装，但签名 7 天后过期，要重新安装；付费的开发者账号有效期一年。

1. 用 Xcode 打开 `native/Jurvis.xcodeproj`。
2. 选 `JurvisMobile` 目标 → Signing & Capabilities → 勾选 Automatically manage signing，Team 选你自己的。
3. 包名 `app.jurvis.mobile` 可能已被别人占用。在 `scripts/generate_xcode.py` 里把 `app.jurvis.` 改成你自己的前缀（例如 `com.<你的名字>.jurvis.`），重新运行脚本。
4. 用数据线连上手机，在顶部选择它，点运行。第一次需要在手机的 **设置 → 通用 → VPN 与设备管理** 里信任你的开发者证书，并在 **隐私与安全性** 里打开开发者模式。

装好后，在 Mac 的 Jurvis 里 **设置 → 配对 iPhone 或 iPad** 创建邀请，用手机 App 扫码。手机会问是否允许访问本地网络，选允许。要在外面用，两边装 Tailscale，见[部署引擎](DEPLOY.md#从其他设备访问)。

**不要把你的团队 ID 或改过的包名提交回仓库。** 团队 ID 在 Xcode 里选择时会写进 `project.pbxproj`；提交前重新运行 `scripts/generate_xcode.py` 可以把工程文件恢复成不带个人信息的状态。

## TestFlight

要分发给其他人的 iPhone，走 TestFlight，需要付费的 Apple 开发者账号：

1. 按上面的第 3 步换成你自己的包名。
2. 在 [App Store Connect](https://appstoreconnect.apple.com) 用这个包名新建一个 App 记录。
3. 在 Xcode 的设置里登录你的 Apple 账号。
4. 在 `.env` 里写上团队 ID（`.env` 不会被提交）：

   ```bash
   JURVIS_TEAM_ID=<你的团队 ID>
   ```

5. 运行：

   ```bash
   ./scripts/testflight.sh
   ```

脚本归档、签名并上传，版本号用当前时间，每次上传都不重复。处理完成后在 App Store Connect 的 TestFlight 页面添加测试人员。

通过 TestFlight 装到的 App 仍然需要连接一台运行引擎的 Mac，它自己不包含模型。

## 验证构建

```bash
./scripts/ui-press.sh
```

```bash
./scripts/ui-smoke.sh
```

`ui-press.sh` 不需要引擎，用真实的鼠标事件检查按钮反馈。`ui-smoke.sh` 连接正在运行的引擎，离屏渲染 Mac 版的每个页面，截图存到 `build/ui-smoke/`。两者都只覆盖 Mac 版；iPhone 版目前靠模拟器里手动检查。
