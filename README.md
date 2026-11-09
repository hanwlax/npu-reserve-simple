# NPU 机时预约 · 简版

面向内网的单机预约服务。使用 Python 3.10+、SQLite 和原生 HTML、CSS、JavaScript。

## 启动与配置

编辑项目里的 `config.json`，填入实际可预约的机器。仓库中的配置初始为空。

```json
{
  "machines": [
    {"id": "a5-01", "name": "A5-01", "ip": "192.0.2.11", "spec": "8 卡 · 1 TB"}
  ]
}
```

`id`、`name` 和 `ip` 必填，`spec` 可省略。每台机器的 `id` 和 IP 须唯一。修改机器名称或 IP 时保留 `id`，原预约仍关联该机器。移除机器会释放该机器尚未开始的预约时段，保留已发生的记录。服务在启动时校验并应用配置。

```bash
python3 server.py serve
```

默认监听 `0.0.0.0:8080`，数据库位于 `data/reservations.sqlite3`。同网段设备访问 `http://<服务器 IP>:8080`。可通过 `--db` 和 `--config` 指定数据库和配置文件，或用 `serve --host` 和 `--port` 调整监听地址。同机部署完整版时，给两套服务配置不同端口。

## 预约操作

1. 输入本人职工号即可登录，例如 `h00893113`。格式为一个英文字母加八位数字，大小写视为同一工号。首次登录会自动建立账户。服务仅校验工号格式，预约和释放权限按输入的工号识别。
2. 在「机器预约」同一行点击或拖选空闲时段后提交。时间轴使用北京时间，从当天 08:00 到次日 08:00，以 30 分钟为单位。并发提交同一时段时，最先提交成功的一单生效。
3. 在「我的预约」选择「释放机时」，可释放整段尚未使用的预约或仅释放一段。也可以直接拖选本人预约中的一段。
4. 点击「历史预约」查看最近 7 天已经开始的预约，列表展示机器、预约人、起止时间和状态。正在使用的预约也会列出。开始前已释放的时段不会出现在历史中。

## systemd 部署

首次安装时从源码目录执行：

```bash
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin npu-reserve-simple
sudo install -d -m 0755 /opt/npu-reserve-simple/static
sudo install -m 0644 server.py reservation.py /opt/npu-reserve-simple/
sudo install -m 0644 static/index.html static/app.css static/app.js /opt/npu-reserve-simple/static/
sudo install -d -m 0750 -o npu-reserve-simple -g npu-reserve-simple /var/lib/npu-reserve-simple
sudo install -m 0640 -o npu-reserve-simple -g npu-reserve-simple config.json /var/lib/npu-reserve-simple/config.json
sudo install -m 0644 deploy/npu-reserve-simple.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now npu-reserve-simple.service
```

后续编辑 `/var/lib/npu-reserve-simple/config.json` 后，运行 `sudo systemctl restart npu-reserve-simple.service`。更新源码时重新安装 Python 和静态文件，再重启服务。旧版简版数据库会在启动时自动迁移，保留原工号、显示名和预约记录。若要改端口，可在 `/etc/npu-reserve-simple.env` 设置 `NPU_RESERVE_PORT=8081` 后重启。

## 打包迁移到另一台 Linux 主机

在旧机停服，将程序、配置、数据库和 systemd unit 打进同一个归档：

```bash
sudo systemctl stop npu-reserve-simple.service
sudo sh -c 'umask 077; tar -C / -czf /root/npu-reserve-simple.tar.gz opt/npu-reserve-simple var/lib/npu-reserve-simple etc/systemd/system/npu-reserve-simple.service'
```

通过受控渠道把归档传到新机。如果旧机配置了 `/etc/npu-reserve-simple.env`，一并复制。在新机创建同名系统账户。如果目标机已有简版部署，先停止服务并将其状态目录移走备份，再恢复归档：

```bash
sudo tar -C / -xzf /path/to/npu-reserve-simple.tar.gz
sudo chown -R npu-reserve-simple:npu-reserve-simple /var/lib/npu-reserve-simple
sudo systemctl daemon-reload
sudo systemctl enable --now npu-reserve-simple.service
sudo systemctl status npu-reserve-simple.service --no-pager
```

用原账号登录新机，核对预约和历史后切换访问地址。切换期间保持旧服务停止。这个流程适用于简版之间的迁移，完整版的数据库使用独立结构。

## 验证

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
systemd-analyze verify deploy/npu-reserve-simple.service
```
