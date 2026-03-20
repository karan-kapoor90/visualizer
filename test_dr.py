import os
from kubernetes import client, config, dynamic
from kubernetes.client import api_client

try:
    config.load_incluster_config()
except config.ConfigException:
    kubeconfig_file = os.getenv("KUBECONFIG")
    if kubeconfig_file:
        config.load_kube_config(config_file=kubeconfig_file)
    else:
        config.load_kube_config()

dyn_client = dynamic.DynamicClient(api_client.ApiClient())
dr_api = dyn_client.resources.get(api_version="v1beta1", group="networking.istio.io", kind="DestinationRule")
drs = dr_api.get()
for dr in drs.items:
    dr_dict = dr.to_dict() if hasattr(dr, 'to_dict') else dr
    name = dr_dict.get('metadata', {}).get('name', 'Unknown')
    spec = dr_dict.get('spec', {})
    distribute = spec.get('trafficPolicy', {}).get('loadBalancer', {}).get('localityLbSetting', {}).get('distribute', 'Not set')
    print(f"DestinationRule '{name}' allocate: {distribute}")
