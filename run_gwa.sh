#!/bin/bash

if [[ $UID != 0 ]]; then
    echo "Please run this script with sudo:"
    echo "sudo $0 $*"
    exit 1
fi

echo "Starting Realm Gateway as gwa.demo"
cd src
./rgw.py  --name gwa.demo                                                    \
          --dns-cname-soa    cname-gwa.demo.                                 \
          --dns-soa gwa.demo. 0.168.192.in-addr.arpa. 1.64.100.in-addr.arpa. \
          --dns-server-local 127.0.0.1 53                                    \
          --dns-server-lan   192.168.1.1 53                                  \
          --dns-server-wan   10.1.3.101 53                                   \
          --dns-resolver     10.1.3.181 53                                   \
          --ddns-server      127.0.0.2 53                                    \
          --dns-timeout      0.010 0.100 0.200                               \
          --dns-timeout-naptr  0.100 0.200 0.300                             \
          --pool-serviceip   10.1.3.101/32                                   \
          --pool-cpoolip     10.1.3.111/32 10.1.3.112/32 10.1.3.113/32       \
          --pool-cespoolip   172.16.1.100/19                                 \
          --ipt-cpool-queue  1                                               \
          --ipt-cpool-chain  CIRCULAR_POOL                                   \
          --ipt-host-chain   CUSTOMER_POLICY                                 \
          --ipt-host-unknown CUSTOMER_POLICY_ACCEPT                          \
          --ipt-policy-order PACKET_MARKING NAT mREJECT ADMIN_PREEMPTIVE     \
                             GROUP_POLICY CUSTOMER_POLICY                    \
                             ADMIN_POLICY ADMIN_POLICY_DHCP                  \
                             ADMIN_POLICY_HTTP ADMIN_POLICY_DNS              \
                             GUEST_SERVICES                                  \
          --ips-hosts        IPS_SUBSCRIBERS                                 \
          --ipt-markdnat                                                     \
          --ipt-flush                                                        \
          --network-api-url  http://127.0.0.1:8081/                          \
          --repository-subscriber-folder ../config.d/gwa.demo.subscriber.d/  \
          --repository-policy-folder     ../config.d/gwa.demo.policy.d/      \
          --spm-services-boolean    False \
          --spm-url-cetp-host   	http://10.0.3.200/API/cetp_policy_node?  \
          --spm-url-cetp-network  	http://10.0.3.200/API/ces_policy_node?   \
          --cetp-config  		../config.d/gwa.demo.cetp.policy/config_gwa.yaml   \
          --cetp-policies  		../config.d/gwa.demo.cetp.policy/cetp_policies.json \
          --repository-api-url  	http://10.0.3.200:8001                   \
          --synproxy         		127.0.0.1 12345
