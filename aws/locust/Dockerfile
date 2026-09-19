FROM docker.io/locustio/locust:2.40.4

COPY locustfile.py /mnt/locust/locustfile.py

WORKDIR /mnt/locust

CMD ["-f", "/mnt/locust/locustfile.py", "--headless", "--users", "5", "--spawn-rate", "1"]