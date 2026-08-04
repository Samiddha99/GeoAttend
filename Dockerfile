# ARG PYTHON_VERSION=3.10-slim-buster
ARG PYTHON_VERSION=3.12
ARG DEBIAN_FRONTEND=noninteractive

FROM python:${PYTHON_VERSION}-slim

ARG PORT
ENV PORT=$PORT

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# EXPORT BUILDKIT_PROGRESS=plain

# Display OS Info
RUN cat /etc/*-release

RUN echo "CPU Architecture:" && uname -m && echo "\n\nCPU Info:" && lscpu


RUN pwd
RUN ls

RUN mkdir -p /code

WORKDIR /code

#install the linux packages, since these are the dependencies of some python packages
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    libpcre2-dev\
    curl \
    gcc \
    cron \
    # wkhtmltopdf \
    supervisor \
    gunicorn3 \
    nginx \
    wget \
    xfonts-75dpi \
    xfonts-base
    # && rm -rf /var/lib/apt/lists/* !
RUN apt-get install -yqq daemonize dbus-user-session fontconfig

# RUN apt-get install -y wkhtmltopdf

# RUN wget https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-2/wkhtmltox_0.12.6.1-2.bullseye_amd64.deb && \
#     apt-get install -y ./wkhtmltox_0.12.6.1-2.bullseye_amd64.deb && \
#     rm wkhtmltox_0.12.6.1-2.bullseye_amd64.deb


# RUN systemctl status

# RUN daemonize /usr/bin/unshare --fork --pid --mount-proc /lib/systemd/systemd --system-unit=basic.target
# RUN exec nsenter -t $(pidof systemd) -a su - $LOGNAME
# RUN /etc/init.d/dbus start

# RUN systemctl status

RUN pip install wheel
RUN pip install gevent
COPY requirements.txt /tmp/requirements.txt
# RUN python -m venv venv
# RUN . venv/bin/activate
RUN set -ex && \
    pip install --upgrade pip && \
    pip install -r /tmp/requirements.txt --upgrade

COPY . /code

RUN pwd
RUN ls
RUN whoami
RUN pip show gunicorn 

RUN rm -rf /etc/nginx/sites-available/default
RUN rm -rf /etc/nginx/sites-enabled/default

COPY Nginx /etc/nginx/sites-available/django_nginx
COPY Nginx /etc/nginx/nginx.conf

COPY gunicorn.socket /etc/systemd/system/gunicorn.socket
COPY gunicorn.service /etc/systemd/system/gunicorn.service

# COPY supervisor.conf /etc/supervisor/conf.d/supervisor.conf

# Activate the nginx configuration
RUN ln -s /etc/nginx/sites-available/django_nginx /etc/nginx/sites-enabled/

# RUN systemctl start gunicorn.socket
# RUN systemctl enable gunicorn.socket
# RUN systemctl status gunicorn.socket
# RUN systemctl status gunicorn
# RUN systemctl daemon-reload
# RUN systemctl restart gunicorn

# Restart nginx and allow the changes to take place
# RUN systemctl enable nginx.service
# RUN systemctl restart nginx

# Test your Nginx configuration for syntax errors
RUN nginx -t

# RUN service supervisor start
# RUN supervisorctl reload
# RUN supervisorctl update

# open the firewall to normal traffic on port 80. Since you no longer need access to the development server, you can remove the rule to open port 8080 as well.
# RUN ufw delete allow 8080
# RUN ufw allow 'Nginx Full'

#public the port so that it can access over the internet
RUN echo "Exposing PORT $PORT"
EXPOSE $PORT

# No makemigrations here: migrations are source code and belong in the repo,
# reviewed. Generating them during a build means the schema changes silently
# and differently on every deploy.
#
# No migrate here either — that needs the production database, and a build
# should never hold those credentials. start.sh runs it at boot instead.
#
# collectstatic does belong in the build: it only writes into the image.
RUN python manage.py collectstatic --noinput

RUN chmod +x start.sh

CMD ["sh", "./start.sh"]