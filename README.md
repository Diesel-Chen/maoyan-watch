# maoyan-watch

猫眼影院新增排片监控。按影片和影厅筛选所有未来日期，发现新日期或加场后，通过飞书、钉钉自定义机器人发送合并通知。不执行选座、下单或支付。

## 环境

Python 3.9+，macOS 或 Linux（使用 `fcntl` 文件锁），仅依赖 Python 标准库。Linux 系统需安装时区数据库。

## 配置和运行

```sh
cp config.example.json config.local.json
chmod 600 config.local.json
```

编辑 `config.local.json`：飞书填写 `FEISHU_WEBHOOK`；钉钉填写 `DINGTALK_WEBHOOK` 和 `DINGTALK_SECRET`（SEC 开头的加签密钥）。至少配置一个渠道。也支持同名环境变量，环境变量优先。

真实配置仅留在本机，已被 `.gitignore` 排除。不要将机器人 URL、访问令牌或加签密钥填入源码、示例文件、Issue 或日志截图。

在 `watch.py` 顶部调整 `CINEMA_ID`、`CINEMA_URL`、`MOVIE_NAME`、`MOVIE_ID`、`WANT_HALL`；通知中的影院名称也需按实际影院修改。默认示例为前滩 MOViE MOViE、《奥德赛》及 IMAX。

```sh
python3 watch.py
```

后台运行：

```sh
nohup python3 -u watch.py >>runner.log 2>&1 &
echo $! > watch.pid
```

停止本次后台进程：

```sh
kill -INT "$(cat watch.pid)"
```

## 监控行为

- 第一次成功抓取只建立已有排片基线，不发送历史通知；之后持续检测新增场次。
- 按日期、时间、影厅和版本去重，渠道分别记录送达状态；失败渠道后续重试，已成功渠道不重复发送。
- 同批新增场次合并，最多每 10 场一条；各日期汇总时间，底部只留一个影院购票入口。
- 只发送放票消息。启动、心跳和异常不发群消息；异常及运行情况写入本地日志。
- 所有时段按北京时间：冲刺 20±5 秒、常规 60±20 秒、夜间 25±5 分钟；具体窗口见源码。
- 检测到风控时退避降级；发现时间是轮询首次观察时间，不代表影院精确发布时间。
- `continuous_state.json` 用于重启去重，请保留；删掉后会重新建立基线。新启用渠道不会补发旧格式历史记录。
- 请求已被服务端接收但响应丢失、或成功发送后写盘失败时，重试仍可能重复送达，不能保证严格恰好一次。
- 单进程文件锁防止同目录重复启动；机器休眠、断网或关闭后无法持续监控。

影院接口属于外部依赖，可能变化。请合理调整轮询频率并遵守平台使用规则。

## 测试

```sh
python3 -m unittest -v test_watch
```

测试使用模拟数据和通知接口，不会向机器人发送消息。真实配置、排片状态、日志及旧备份不进入版本库。
