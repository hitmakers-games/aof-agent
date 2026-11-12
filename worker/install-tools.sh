#!/bin/sh
set -eu
kubectl_version=v1.36.2
helm_version=v3.18.4
curl -fsSL --retry 1 "https://dl.k8s.io/release/$kubectl_version/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl
curl -fsSL --retry 1 "https://dl.k8s.io/release/$kubectl_version/bin/linux/amd64/kubectl.sha256" -o /tmp/kubectl.sha256
cd /usr/local/bin
printf '%s  kubectl\n' "$(cat /tmp/kubectl.sha256)" | sha256sum -c -
chmod 755 /usr/local/bin/kubectl
cd /tmp
curl -fsSL --retry 1 "https://get.helm.sh/helm-$helm_version-linux-amd64.tar.gz" -o "helm-$helm_version-linux-amd64.tar.gz"
curl -fsSL --retry 1 "https://get.helm.sh/helm-$helm_version-linux-amd64.tar.gz.sha256sum" -o helm.sha256
sha256sum -c helm.sha256
tar -xzf "helm-$helm_version-linux-amd64.tar.gz"
mv linux-amd64/helm /usr/local/bin/helm
rm -rf linux-amd64
rm -f "helm-$helm_version-linux-amd64.tar.gz" helm.sha256 kubectl.sha256
