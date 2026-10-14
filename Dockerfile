FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PORT=8000

# 大陆地域构建：换成腾讯云镜像源，否则 apt / pip 经常超时
RUN sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g; s|security.debian.org|mirrors.cloud.tencent.com|g' \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true

# tesseract + 中文简体语言包（截图 OCR 用）；tzdata 让容器内的北京时间生效
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tzdata \
        curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple/ \
        --trusted-host mirrors.cloud.tencent.com -r requirements.txt

COPY app ./app
COPY config ./config
COPY README.md ./

# HUB 平台端（渔货栈供应链中台）：仓库根的 src/ · templates/ · static/，
# 与自营端跑在同一个进程、同一个端口下（见 app/main.py 的 /hub 挂载）
COPY src ./src
COPY templates ./templates
COPY static ./static
COPY scripts ./scripts

# 货品检测入库出库管理（检测仓）：**独立业务系统**，有自己的 Base / 菜单 / 角色权限 / 模板，
# 被 HUB 平台端挂载进同一个进程（数据互通）；也可以单独起（python -m wms.main）。
COPY wms ./wms

# 数据库文件与订单截图的落盘位置（docker-compose 会挂卷）
RUN mkdir -p /app/data/media

EXPOSE 8000

# 单进程：后台自动同步线程只在主进程里跑一份
CMD ["python", "-m", "app.cli", "serve"]
