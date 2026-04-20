# ruststudy-mirror

RustStudy 的 Windows 二进制镜像源。

本仓库自动镜像 PHP / Nginx / Apache / Redis 的 Windows 官方发行版，通过 jsDelivr CDN 加速分发，让国内用户秒装这些组件。

## 架构

```
官方上游 (php.net, nginx.org, apache lounge, github)
        |
        v
 GitHub Actions (每天定时 + 手动触发)
        |  auto-discover + download + upload
        v
 本仓库 Releases (按 {software}-{version} 打 tag)
        |
        v
 jsDelivr CDN -> 用户 RustStudy
```

## 工作原理

1. `.github/workflows/mirror.yml` 每天凌晨 02:00 UTC（北京 10:00）运行 `scripts/sync.py`
2. 脚本查询各上游最新版本清单（php.net 的 releases.json、nginx.org 下载页、Apache Lounge、tporadowski/redis 的 GitHub Releases）
3. 对比本仓库已有的 Release，下载 + 校验 sha256 + 创建 Release + 上传 zip
4. 生成 `manifest.json`，客户端读这个拿最新版本列表

## 访问方式

```
主: https://cdn.jsdelivr.net/gh/OverfireWater/ruststudy-mirror@{tag}/{filename}
备: https://github.com/OverfireWater/ruststudy-mirror/releases/download/{tag}/{filename}
```

## 手动触发同步

1. 打开 Actions 页面
2. 选 `Mirror packages` -> `Run workflow`
3. 可选勾 `force` 强制重下所有版本
4. 2-10 分钟完成

## 可选配置

根目录加 `config.yaml` 做黑白名单：

```yaml
php:
  include_branches: ["8.2", "8.3", "8.4"]
nginx:
  exclude_versions: ["1.27.0"]
```

没有 config.yaml 就是全都要。
