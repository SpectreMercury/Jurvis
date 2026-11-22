# 健康数据和心理陪伴

Jurvis 可以读取睡眠、运动、心率、心率变异性（HRV）、恢复、心情和经期，回答「我这周睡得怎么样」「下次例假什么时候」这类问题。所有数据都只存在 Mac 上的 `data/jurvis.db`。

## 隐私

- 模型只能通过 `health_summary` 看到一段时间的平均值和总数，看不到任何单条记录。
- 经期记录（日期、症状、预测）只交给本机模型：`cycle_status` 不会提供给云端模型，以前读过的结果在换成云端模型后也会被遮住。
- 「身体压力」是估算值。它把最近三天的心率变异性、静息心率和睡眠，与你自己 30 天的平均值做比较，不是医学测量。
- 经期预测只根据你自己的记录计算，不能用于避孕。

## 在哪里连接

「设置 → 健康与运动」和「健康」页面顶部都有同样的四个连接器：

| 连接器 | 怎么连接 |
| --- | --- |
| Apple 健康 | 在 iPhone 上点「连接」，允许读取 |
| Apple 健身 | 跟 Apple 健康一起同步，不用单独连接 |
| WHOOP | 先填一次你自己的开发者应用，再点「连接」 |
| Google Health | 同上；Fitbit 和 Pixel Watch 的数据从这里来 |

## Apple 健康和 Apple 健身

Apple 健康没有网页接口，所以由 iPhone 上的 Jurvis 读取：

1. 在 iPhone 上打开 Jurvis，进入「设置 → 健康与运动」，在 Apple 健康那一行点「连接」，允许读取。
2. 第一次会同步最近 30 天的数据，以及最近 180 天的经期记录。之后有新的睡眠、HRV、静息心率或心情记录时，iOS 会在后台唤醒 Jurvis，马上把最近几天的数据发给 Mac（最多每 5 分钟一次）；每次打开 App 时也会同步。

Apple 健身里的体能训练、活动能量和锻炼分钟都存在 Apple 健康里，所以会一起同步。

会读取的数据：步数、活动能量、锻炼分钟、步行跑步距离、睡眠（含深睡、REM、清醒）、静息心率、HRV、血氧、呼吸率、体重、正念分钟、体能训练、经期流量，以及「心理状态」记录。工程里的 `JurvisMobile.entitlements` 已经打开 HealthKit；用自己的团队签名时，Xcode 会自动给 App ID 加上 HealthKit 能力。

## WHOOP

1. 在 <https://developer-dashboard.whoop.com> 创建一个应用。
   - 权限勾选 `read:recovery`、`read:cycles`、`read:sleep`、`read:workout`、`read:body_measurement` 和 `offline`。
   - 回调地址（Redirect URL）填 `https://localhost:18777/oauth/whoop`。如果设置了 `JURVIS_OAUTH_BASE` 或 `JURVIS_PUBLIC_URL`，就填那个地址加 `/oauth/whoop`。「设置」窗口里会显示准确的地址，可以直接复制。
2. 在「设置 → 健康与运动」的 WHOOP 那一行点「设置」，填 Client ID 和 Client Secret。它们只存在 Mac 上的 `data/security/health.json`，不用重启。也可以写在 `.env` 里（`JURVIS_WHOOP_CLIENT_ID`、`JURVIS_WHOOP_CLIENT_SECRET`），`.env` 优先。
3. 在 Mac 上点「连接」，在浏览器里登录 WHOOP 并授权。回调地址是 `localhost` 时，必须在 Mac 上点，因为浏览器要回到这台 Mac。
   - 本机地址用的是自签名证书，浏览器第一次会提示不安全，选择继续即可。
   - 授权后会同步最近 30 天，之后每 15 分钟同步一次。

会读取的数据：恢复分数、静息心率、HRV、血氧、睡眠（各阶段和睡眠表现）、每日负荷（strain），以及训练记录。

## Google Health

Google Health API 取代了旧的 Fitbit Web API 和 Google Fit，覆盖 Fitbit 和 Pixel Watch 的数据。

1. 在 Google Cloud 里启用 Google Health API。在「API 和服务 → 凭据」里创建一个「Web 应用」类型的 OAuth 客户端，重定向 URI 填 `https://localhost:18777/oauth/google`（或者你的 `JURVIS_OAUTH_BASE` 加 `/oauth/google`）。
2. 在「设置 → 健康与运动」的 Google Health 那一行点「设置」，填客户端 ID 和客户端密钥（或者写在 `.env` 的 `JURVIS_GOOGLE_CLIENT_ID`、`JURVIS_GOOGLE_CLIENT_SECRET`）。
3. 在 Mac 上点「连接」。

会读取的数据：步数、睡眠、每日静息心率、每日 HRV、运动记录。Google Health API 的经期数据只能写入、不能读取，所以经期请在 Apple 健康里记录，或者直接告诉 Jurvis。

## 经期

- 可以直接说「今天来例假了，有点痛经」，或「例假 11 月 3 号到 7 号」。Jurvis 会调用 `log_period` 记录下来，不会再问一遍。
- 也可以在「健康」页点「今天来了 / 今天结束了」。
- Apple 健康同步过来的记录，不会覆盖你自己说过或手动记过的那些天。

## 心理陪伴

如果你在一个讲别的事情的对话里开始说心事（压力、情绪低落、焦虑、感情、家庭、孤独、倦怠），Jurvis 会调用 `open_session`，单独开一个安静的对话，带着你刚才说过的内容和一句开场白，App 会直接切过去。原来的对话里只留一行说明和一张可以点回去的卡片。

在这个陪伴对话里，Jurvis 会这样做：

- 先听，复述你的感受，不说「想开点」这类套话，一次只问一个问题。
- 你想要办法时，再一次提供一种：着地练习、呼吸、换个角度看一个想法、一件很小的下一步、自我关怀。
- 和身体状况有关时，会参考睡眠和身体压力数据。经你同意后，可以用 `log_mood` 记下当天的心情。
- 不做诊断，不给用药建议。如果困扰持续两周以上或影响到生活，会建议你去找专业人士。

如果提到不想活、想伤害自己或别人，Jurvis 不会开新对话，而是当场回应，问你现在是否安全，并给出求助热线：心理援助热线 12356、希望24热线 400-161-9995、北京心理危机研究与干预中心 010-82951332；紧急情况请拨 120 或 110。

陪伴对话的完整说明在 `engine/prompts/companion.md`。

## 邮件

在「设置 → 邮件」里点一个邮箱。

| 邮箱 | 怎么连接 |
| --- | --- |
| Gmail | 用 Google 账号登录授权（只读权限 `gmail.readonly`），不用输入密码 |
| Outlook、Hotmail、Microsoft 365 | 用 Microsoft 账号登录授权（只读权限 `Mail.Read`） |
| iCloud 邮件 | App 专用密码 |
| QQ 邮箱、网易 163 / 126 | 授权码 |
| 其他 IMAP 邮箱 | 服务器地址加专用密码 |

为什么有的要专用密码：Apple、腾讯和网易都没有给第三方 App 用的登录授权，只能在它们的网站上生成一个专用密码或授权码。它只能用来收邮件，不是你的账户密码，随时可以在它们的网站上作废。窗口里有生成步骤和链接。

登录授权需要你自己的开发者应用（和 WHOOP、Google Health 一样，只需填一次）：

- **Gmail**：和 Google Health 共用同一个 Google 应用。在 Google Cloud 里启用 Gmail API，在「OAuth 同意屏幕」里加上 `gmail.readonly` 权限，并把自己加为测试用户。重定向 URI 是 `https://localhost:18777/oauth/google`。
- **Outlook**：在 Microsoft Entra 管理中心「应用注册」里新建一个应用，账户类型选「任何组织目录中的帐户和个人 Microsoft 帐户」，重定向 URI（Web）填 `https://localhost:18777/oauth/microsoft`，API 权限加 Microsoft Graph 的 `Mail.Read` 和 `offline_access`，再新建一个客户端密码。也可以写在 `.env`：`JURVIS_MICROSOFT_CLIENT_ID`、`JURVIS_MICROSOFT_CLIENT_SECRET`。
- 开发者应用存在 `data/security/oauth_apps.json`，登录后的令牌和专用密码存在 `data/security/mail.json`，都只有你自己能读，接口不会返回它们。
- 回调地址是 `localhost` 时，要在 Mac 上点登录。

其他：

- 只读：只看邮件，不发送，不移动，也不标记已读。
- 默认只有本机模型能读邮件。每个邮箱都可以单独改成「本机和云端模型都能读」。
- 工具：`mail_recent` 列出最近的邮件（发件人、主题、时间、开头几行），`mail_read` 读取一封全文。

## 主动关心

Jurvis 是会主动找你的助理，不只是等你提问。新的身体数据一到（iPhone、WHOOP、Google 同步，或你记下心情），引擎马上检查；没有新数据时也每 5 分钟看一次。出现下面任一信号，就会主动问候你：

- 身体压力估算「偏高」：HRV 比平时低 15% 以上、静息心率比平时高 3 次/分以上、最近平均睡眠不足 6 小时，满足其中两条。
- 连续两晚睡眠不足 6 小时。
- WHOOP 恢复低于 34%。
- 最近三天记录的心情平均 2 分以下。

问候分两步：

1. 本机模型先看最近几天的健康汇总、最近三天的邮件，以及你在其他对话里说过的话（`recent_conversations`），判断有没有可能的原因。
2. 然后写一条简短的问候，发到陪伴对话里（三天内有就沿用，没有就新开一个「最近还好吗」）。
   - 找到可能的原因时，用猜测的口吻轻轻提一句，不会罗列你的邮件或数据。
   - 找不到原因时，就只是关心你。

限制：

- 新出现的信号马上发。还在持续的信号不重复说；消失后又出现，算新的。
- 安静时段（默认 22:30–08:30）先攒着，结束后再发。你正在和 Jurvis 说话时（10 分钟内发过消息）也先等一等。
- 两条问候之间至少隔 1 小时，免得信号来回跳时一直发。
- 最近三小时你在陪伴对话里聊过，期间出现的信号算已经知道，不再另外问候。
- 最近一天没有同步过身体数据时，不根据旧数据猜。

问候写好后会弹通知，点开直接进入那段对话：

- Mac App 一直开着，所以写好就会弹出。
- 网页版：打开「主动关心」开关时会请求浏览器通知权限；页面开着时弹系统通知，没有权限就在页面里提示。
- iPhone：App 在前台或回到前台时弹出。App 完全关掉后要等下次打开才会看到，因为真正的远程推送需要 Apple 推送服务（APNs）和一个转发服务器，现在还没有做。

整个过程固定使用本机模型。可以在「设置 → 主动关心」里关闭、修改安静时段，或点「现在看看」立即检查一次。
