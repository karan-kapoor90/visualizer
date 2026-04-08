import os
import asyncio
import time
import json
import urllib.request
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from kubernetes import client, config, dynamic
from kubernetes.client import api_client
import uvicorn
import yaml

app = FastAPI()
# WebSocket Connection Manager
active_connections: list[WebSocket] = []

async def broadcast_message(message: str):
    for connection in active_connections:
        try:
            await connection.send_text(message)
        except Exception:
            pass

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    print("New websocket connection")
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.remove(websocket)

@app.websocket("/ws/yaml")
async def websocket_yaml_endpoint(websocket: WebSocket):
    print("New websocket YAML connection")
    await websocket.accept()
    try:
        kubeconfig_file = os.getenv("KUBECONFIG")
        if not kubeconfig_file and os.path.exists("kdp_kube_config"):
            kubeconfig_file = "kdp_kube_config"
            
        # Read context from query parameters if supplied
        target_context = websocket.query_params.get("context", "")
        if not target_context or target_context == "current":
             try:
                 _, active = config.list_kube_config_contexts()
                 target_context = active['name'] if active else 'delhi'
             except Exception:
                 target_context = 'delhi'

        print(f"WS YAML using context: {target_context}")
        api_client_ctx = config.new_client_from_config(config_file=kubeconfig_file, context=target_context)
        dyn_client_ctx = dynamic.DynamicClient(api_client_ctx)
        
        while True:
            try:
                dr_api = dyn_client_ctx.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
                vs_api = dyn_client_ctx.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="VirtualService")
                
                dr = dr_api.get(name="whereami", namespace=NAMESPACE)
                vs = vs_api.get(name="whereami-vs", namespace=NAMESPACE)
                
                dr_dict = dr.to_dict()
                if 'metadata' in dr_dict:
                    if isinstance(dr_dict['metadata'], dict):
                        dr_dict['metadata'].pop('annotations', None)
                        dr_dict['metadata'].pop('managedFields', None)
                dr_yaml = yaml.dump(dr_dict)

                vs_dict = vs.to_dict()
                if 'metadata' in vs_dict:
                    if isinstance(vs_dict['metadata'], dict):
                        vs_dict['metadata'].pop('annotations', None)
                        vs_dict['metadata'].pop('managedFields', None)
                vs_yaml = yaml.dump(vs_dict)
                
                await websocket.send_json({
                    "dr": dr_yaml,
                    "vs": vs_yaml
                })
            except Exception as e:
                await websocket.send_json({"error": str(e)})
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        print("Websocket YAML disconnected")


# Initialize Kubernetes Client
try:
    # Use in-cluster config when deployed to GKE
    config.load_incluster_config()
    print('--local config based connect--')
except config.ConfigException:
    # Fallback to local kubeconfig for development
    kubeconfig_file = os.getenv("KUBECONFIG")
    if not kubeconfig_file and os.path.exists("kdp_kube_config"):
        kubeconfig_file = "kdp_kube_config"
        
    if kubeconfig_file:
        config.load_kube_config(config_file=kubeconfig_file)
        print(f'--fallback to custom kubeconfig: {kubeconfig_file}--')
    else:
        config.load_kube_config()
        print('--fallback to default kubeconfig--')

# Verify connection by listing available contexts
try:
    contexts, active_context = config.list_kube_config_contexts()
    if contexts:
        print("Available contexts:")
        for context in contexts:
            print(f" - {context['name']}")
    else:
        print("Cannot find any contexts in kube-config file.")
except Exception as e:
    print(f"Could not list contexts (might be using in-cluster config or file missing): {e}")



dyn_client = dynamic.DynamicClient(api_client.ApiClient())

# Istio API Details
ISTIO_GROUP = "networking.istio.io"
ISTIO_VERSION = "v1beta1"
NAMESPACE = os.getenv("APP_NAMESPACE", "default")

# Workload Configuration Globals
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
APP_NAMESPACE = os.getenv("APP_NAMESPACE", "")
APP_LABEL = os.getenv("APP_LABEL", "")
PRIMARY_CLUSTER = os.getenv("PRIMARY_CLUSTER", "")

class WorkloadConfig(BaseModel):
    gcp_project_id: str
    app_namespace: str
    app_label: str

class PrimaryCluster(BaseModel):
    primary_cluster: str

class TrafficConfig(BaseModel):
    weight_mumbai: int
    failover_enabled: bool
    outlier_detection_enabled: bool

class TrafficGenConfig(BaseModel):
    service_name: str
    rps: int
    duration: int
    feature_flag: str = None

# Global state for traffic generation
traffic_task = None
traffic_running = False

async def generate_traffic_loop(service_url: str, rps: int, duration: int, feature_flag: str = None):
    global traffic_running
    delay = 1.0 / rps if rps > 0 else 1.0
    start_time = time.time()
    
    while traffic_running:
        if time.time() - start_time >= duration:
            traffic_running = False
            break
            
        try:
            def fetch():
                req = urllib.request.Request(service_url)
                if feature_flag:
                    req.add_header('x-feature-flag', feature_flag)
                with urllib.request.urlopen(req, timeout=2.0) as response:
                    body = response.read().decode('utf-8')
                    try:
                        data = json.loads(body)
                        return f"JSON Response: {json.dumps(data)}"
                    except json.JSONDecodeError:
                        return f"Response: {body}"
                        
            msg = await asyncio.to_thread(fetch)
            if msg:
                print(msg)
                await broadcast_message(msg)
        except Exception as e:
            # We expect some errors if the service is down (e.g., 503) during our chaos test
            pass
        await asyncio.sleep(delay)

@app.get("/api/contexts")
async def get_contexts():
    try:
        contexts, active_context = config.list_kube_config_contexts()
        names = [c['name'] for c in contexts]
        active = active_context['name'] if active_context else ""
        return {"contexts": names, "active": active}
    except Exception as e:
        return {"contexts": [], "active": "", "error": str(e)}

@app.post("/api/generateTraffic")
async def start_traffic(cfg: TrafficGenConfig):
    global traffic_task, traffic_running
    
    # Prefix with http if missing
    url = cfg.service_name
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
        
    if traffic_running and traffic_task:
        traffic_running = False
        await asyncio.sleep(0.5) # Wait briefly for loop to terminate

    traffic_running = True
    print(f"Starting traffic generation for service: {url} at {cfg.rps} RPS for {cfg.duration}s")
    traffic_task = asyncio.create_task(generate_traffic_loop(url, cfg.rps, cfg.duration, cfg.feature_flag))
    return {"status": "success", "message": f"Traffic started to {url} at {cfg.rps} RPS for {cfg.duration}s"}

@app.post("/api/stopTraffic")
async def stop_traffic():
    global traffic_task, traffic_running
    traffic_running = False
    if traffic_task:
        traffic_task.cancel()
        traffic_task = None
    return {"status": "success", "message": "Traffic stopped"}

@app.post("/api/workloadConfig")
async def update_workload_config(cfg: WorkloadConfig):
    global GCP_PROJECT_ID, APP_NAMESPACE, APP_LABEL
    GCP_PROJECT_ID = cfg.gcp_project_id
    APP_NAMESPACE = cfg.app_namespace
    APP_LABEL = cfg.app_label
    print(f"Updated Workload Config: Project={GCP_PROJECT_ID}, Namespace={APP_NAMESPACE}, Label={APP_LABEL}")
    return {"status": "success", "message": "Workload configuration updated"}

@app.get("/api/initialConfig")
async def get_initial_config():
    return {
        "gcp_project_id": os.getenv("GCP_PROJECT_ID", ""),
        "app_namespace": os.getenv("APP_NAMESPACE", ""),
        "app_label": os.getenv("APP_LABEL", "")
    }

@app.get("/api/fleetMemberships")
async def get_fleet_memberships():
    try:
        process = await asyncio.create_subprocess_exec(
            'gcloud', 'container', 'fleet', 'memberships', 'list', '--format=json',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            print(f"gcloud error: {stderr.decode()}")
            return {"memberships": [], "error": stderr.decode()}
            
        memberships_data = json.loads(stdout.decode())
        formatted_memberships = []
        for item in memberships_data:
            full_name = item.get('name', '')
            parts = full_name.split('/')
            name = parts[-1] if len(parts) >= 1 else ''
            location = parts[-3] if len(parts) >= 3 else 'unknown'
            
            formatted_memberships.append({
                "name": name,
                "location": location
            })
            
        return {"memberships": formatted_memberships}
    except Exception as e:
        print(f"Error getting fleet memberships: {e}")
        return {"memberships": [], "error": str(e)}

async def poll_mesh_health():
    print("Starting mesh health polling task...")
    while True:
        try:
            memberships_response = await get_fleet_memberships()
            memberships = memberships_response.get('memberships', [])
            
            health_data = []
            for m in memberships:
                name = m['name']
                location = m['location']
                
                process = await asyncio.create_subprocess_exec(
                    'gcloud', 'container', 'fleet', 'memberships', 'describe', name,
                    '--location', location, '--format=json',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                status = "UNKNOWN"
                if process.returncode == 0:
                    try:
                        desc = json.loads(stdout.decode())
                        status = desc.get('state', {}).get('code', 'UNKNOWN')
                    except json.JSONDecodeError:
                        print(f"Error decoding JSON for {name}")
                else:
                    print(f"Error describing {name}: {stderr.decode()}")
                    
                health_data.append({
                    "name": name,
                    "location": location,
                    "status": status
                })
                
            message = json.dumps({
                "type": "health_update",
                "data": health_data
            })
            await broadcast_message(message)
            
        except Exception as e:
            print(f"Error in poll_mesh_health: {e}")
            
        await asyncio.sleep(180)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(poll_mesh_health())

@app.post("/api/primaryCluster")
async def update_primary_cluster(cfg: PrimaryCluster):
    global PRIMARY_CLUSTER
    PRIMARY_CLUSTER = cfg.primary_cluster
    print(f"Updated Primary Cluster: {PRIMARY_CLUSTER}")
    return {"status": "success", "message": "Primary cluster updated"}

@app.get("/api/canary/versions")
async def get_canary_versions():
    global PRIMARY_CLUSTER, APP_NAMESPACE, APP_LABEL
    
    if not PRIMARY_CLUSTER or not APP_NAMESPACE or not APP_LABEL:
        return {"versions": [], "warning": "Missing workload configuration (cluster, namespace, or label)"}
        
    try:
        kubeconfig_file = os.getenv("KUBECONFIG")
        if not kubeconfig_file and os.path.exists("kdp_kube_config"):
            kubeconfig_file = "kdp_kube_config"
            
        print(f"Fetching versions using context: {PRIMARY_CLUSTER}, namespace: {APP_NAMESPACE}, label: app={APP_LABEL}")
        
        api_client_ctx = config.new_client_from_config(config_file=kubeconfig_file, context=PRIMARY_CLUSTER)
        dyn_client_ctx = dynamic.DynamicClient(api_client_ctx)
        
        apps_api = dyn_client_ctx.resources.get(api_version="v1", group="apps", kind="Deployment")
        deps = apps_api.get(namespace=APP_NAMESPACE, label_selector=f"app={APP_LABEL}")
        
        versions = set()
        for dep in deps.items:
            labels = dep.metadata.labels
            if hasattr(labels, 'to_dict'):
                labels = labels.to_dict()
                
            if isinstance(labels, dict):
                if 'featureflag' in labels:
                    continue
                if 'version' in labels:
                    versions.add(labels['version'])
            elif hasattr(labels, 'version'):
                if not hasattr(labels, 'featureflag'):
                    versions.add(labels.version)
                
        # If no deployments found, try Pods as fallback
        if not versions:
            pods_api = dyn_client_ctx.resources.get(api_version="v1", kind="Pod")
            pods = pods_api.get(namespace=APP_NAMESPACE, label_selector=f"app={APP_LABEL}")
            for pod in pods.items:
                labels = pod.metadata.labels
                if hasattr(labels, 'to_dict'):
                    labels = labels.to_dict()
                if isinstance(labels, dict):
                    if 'featureflag' in labels:
                        continue
                    if 'version' in labels:
                        versions.add(labels['version'])
                    
        return {"versions": list(versions)}
    except Exception as e:
        print(f"Error fetching versions: {e}")
        return {"versions": [], "error": str(e)}

class CanarySelection(BaseModel):
    version: str
    weight: int

class CanaryApplyConfig(BaseModel):
    selections: list[CanarySelection]

@app.post("/api/canary/apply")
async def apply_canary_endpoint(cfg: CanaryApplyConfig):
    global PRIMARY_CLUSTER, APP_NAMESPACE, APP_LABEL
    
    if not PRIMARY_CLUSTER or not APP_NAMESPACE or not APP_LABEL:
        raise HTTPException(status_code=400, detail="Missing workload configuration")
        
    try:
        kubeconfig_file = os.getenv("KUBECONFIG")
        if not kubeconfig_file and os.path.exists("kdp_kube_config"):
            kubeconfig_file = "kdp_kube_config"
            
        # Find region from primary cluster context
        contexts, _ = config.list_kube_config_contexts(config_file=kubeconfig_file)
        target_cluster = None
        for ctx in contexts:
            if ctx['name'] == PRIMARY_CLUSTER:
                target_cluster = ctx['context']['cluster']
                break
                
        if not target_cluster:
            raise HTTPException(status_code=400, detail=f"Context {PRIMARY_CLUSTER} not found in kubeconfig")
            
        # Extract region from cluster name (assuming format gke_project_region_name)
        parts = target_cluster.split('_')
        if len(parts) >= 3:
            region = parts[2]
        else:
            region = "asia-south2" # Fallback
            
        print(f"Extracted region: {region} for cluster {target_cluster}")
        
        # Construct DestinationRule patch
        subsets = []
        for sel in cfg.selections:
            subsets.append({
                "name": sel.version,
                "labels": {
                    "app": APP_LABEL,
                    "version": sel.version
                }
            })
            
        dr_patch = {
            "spec": {
                "trafficPolicy": {
                    "loadBalancer": {
                        "localityLbSetting": {
                            "enabled": True,
                            "distribute": [
                                {
                                    "from": "*",
                                    "to": {
                                        f"{region}/*": 100
                                    }
                                }
                            ]
                        }
                    }
                },
                "subsets": subsets
            }
        }
        
        # Construct VirtualService patch
        routes = []
        for sel in cfg.selections:
            routes.append({
                "destination": {
                    "host": f"{APP_LABEL}.{APP_NAMESPACE}.svc.cluster.local",
                    "subset": sel.version
                },
                "weight": sel.weight
            })
            
        vs_patch = {
            "spec": {
                "http": [
                    {
                        "route": routes
                    }
                ]
            }
        }
        
        errors = []
        for ctx in contexts:
            context_name = ctx['name']
            try:
                print(f"Applying Canary config to context: {context_name}")
                api_client_ctx = config.new_client_from_config(config_file=kubeconfig_file, context=context_name)
                dyn_client_ctx = dynamic.DynamicClient(api_client_ctx)
                
                dr_api = dyn_client_ctx.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
                vs_api = dyn_client_ctx.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="VirtualService")
                
                # Patch DestinationRule
                dr_api.patch(name=APP_LABEL, namespace=APP_NAMESPACE, body=dr_patch, content_type="application/merge-patch+json")
                
                # Patch VirtualService
                vs_name = f"{APP_LABEL}-vs"
                if APP_LABEL == "whereami":
                     vs_name = "whereami-vs"
                     
                vs_api.patch(name=vs_name, namespace=APP_NAMESPACE, body=vs_patch, content_type="application/merge-patch+json")
                
            except Exception as patch_e:
                print(f"Error on context {context_name}: {patch_e}")
                errors.append(f"{context_name}: {patch_e}")
                
        if errors:
             return {"status": "partial_success", "message": f"Applied with errors in some contexts", "errors": errors}
             
        return {"status": "success", "message": "Canary deployment applied to all contexts"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("index.html", "r") as f:
        return f.read()

@app.get("/api/destination-rules/distribute")
async def get_dr_distribute():
    try:
        dr_api = dyn_client.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
        drs = dr_api.get()
        distribute_settings = []
        for dr in drs.items:
            try:
                name = dr.metadata.name
                spec = dr.spec
                traffic_policy = getattr(spec, 'trafficPolicy', {}) if hasattr(spec, 'trafficPolicy') else spec.get('trafficPolicy', {})
                load_balancer = getattr(traffic_policy, 'loadBalancer', {}) if hasattr(traffic_policy, 'loadBalancer') else traffic_policy.get('loadBalancer', {})
                locality_lb = getattr(load_balancer, 'localityLbSetting', {}) if hasattr(load_balancer, 'localityLbSetting') else load_balancer.get('localityLbSetting', {})
                distribute = getattr(locality_lb, 'distribute', None) if hasattr(locality_lb, 'distribute') else locality_lb.get('distribute', None)
            except AttributeError:
                dr_dict = dr.to_dict() if hasattr(dr, 'to_dict') else dr
                spec = dr_dict.get('spec', {})
                distribute = spec.get('trafficPolicy', {}).get('loadBalancer', {}).get('localityLbSetting', {}).get('distribute', None)
            
            if distribute and isinstance(distribute, list):
                distribute_settings.extend(distribute)
        
        return {"distribute": distribute_settings}
    except Exception as e:
        return {"error": str(e), "distribute": []}

@app.post("/api/destination-rules/distribute")
async def update_dr_distribute(payload: dict):
    try:
        print(f"Received distribute payload: {payload}")
        dr_api = dyn_client.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
        
        mode = payload.get("mode")
        if mode == "simple_rr":
            print("In the if clause")
            dr_patch = {
                "spec": {
                    "trafficPolicy": {
                        "loadBalancer": {
                            "simple": "ROUND_ROBIN",
                            "localityLbSetting": {
                                "enabled": False
                            }
                        }
                    }
                }
            }
        else:
            distribute_data = payload.get("distribute", {})
            print(distribute_data)
            dr_patch = {
                "spec": {
                    "trafficPolicy": {
                        "loadBalancer": {
                            "localityLbSetting": {
                                "enabled": True,
                                "distribute": [
                                    {
                                        "from": "*",
                                        "to": distribute_data
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        
        errors = []
        contexts, _ = config.list_kube_config_contexts()
        for ctx in contexts:
            context_name = ctx['name']
            try:
                print(f"Applying DestinationRule patch to context: {context_name}")
                api_client_ctx = config.new_client_from_config(context=context_name)
                dyn_client_ctx = dynamic.DynamicClient(api_client_ctx)
                dr_api_ctx = dyn_client_ctx.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
                dr_api_ctx.patch(name="whereami", namespace=NAMESPACE, body=dr_patch, content_type="application/merge-patch+json")
            except Exception as patch_e:
                print(f"Error on context {context_name}: {patch_e}")
                errors.append(f"{context_name}: {patch_e}")
                
        if errors:
            return {"status": "partial_success", "message": f"Applied with errors in some contexts", "errors": errors}
            
        return {"status": "success", "message": "DestinationRule distribute policy applied to all contexts"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/resetPolicies")
async def reset_policies():
    try:
        kubeconfig_file = os.getenv("KUBECONFIG")
        if not kubeconfig_file and os.path.exists("kdp_kube_config"):
            kubeconfig_file = "kdp_kube_config"
            
        contexts, _ = config.list_kube_config_contexts(config_file=kubeconfig_file)
        
        errors = []
        successes = []
        manifest_dir = "manifests/workload/mesh/multi-region"
        
        for ctx in contexts:
            context_name = ctx['name']
            try:
                print(f"Resetting policies in context: {context_name}")
                process = await asyncio.create_subprocess_exec(
                    'kubectl', 'apply', '-f', manifest_dir, '--context', context_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode == 0:
                    successes.append(f"Successfully applied to {context_name}")
                    print(f"kubectl output: {stdout.decode()}")
                else:
                    errors.append(f"Error applying to {context_name}: {stderr.decode()}")
                    print(f"kubectl error: {stderr.decode()}")
                    
            except Exception as e:
                errors.append(f"Exception applying to {context_name}: {str(e)}")
                print(f"Exception for context {context_name}: {e}")
                
        if errors:
            return {
                "status": "partial_success" if successes else "error",
                "message": "Finished with errors",
                "errors": errors,
                "successes": successes
            }
            
        return {"status": "success", "message": "Policies reset to default on all contexts", "successes": successes}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/applyFeatureTesting")
async def apply_feature_testing():
    try:
        kubeconfig_file = os.getenv("KUBECONFIG")
        if not kubeconfig_file and os.path.exists("kdp_kube_config"):
            kubeconfig_file = "kdp_kube_config"
            
        contexts, _ = config.list_kube_config_contexts(config_file=kubeconfig_file)
        
        errors = []
        successes = []
        manifest_dir = "manifests/workload/mesh/featureflag"
        
        for ctx in contexts:
            context_name = ctx['name']
            try:
                print(f"Applying Feature Testing policies in context: {context_name}")
                process = await asyncio.create_subprocess_exec(
                    'kubectl', 'apply', '-f', manifest_dir, '--context', context_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode == 0:
                    successes.append(f"Successfully applied to {context_name}")
                    print(f"kubectl output: {stdout.decode()}")
                else:
                    errors.append(f"Error applying to {context_name}: {stderr.decode()}")
                    print(f"kubectl error: {stderr.decode()}")
                    
            except Exception as e:
                errors.append(f"Exception applying to {context_name}: {str(e)}")
                print(f"Exception for context {context_name}: {e}")
                
        if errors:
            return {
                "status": "partial_success" if successes else "error",
                "message": "Finished with errors",
                "errors": errors,
                "successes": successes
            }
            
        return {"status": "success", "message": "Feature Testing policies applied to all contexts", "successes": successes}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/applyAutoFailover")
async def apply_auto_failover():
    try:
        kubeconfig_file = os.getenv("KUBECONFIG")
        if not kubeconfig_file and os.path.exists("kdp_kube_config"):
            kubeconfig_file = "kdp_kube_config"
            
        contexts, _ = config.list_kube_config_contexts(config_file=kubeconfig_file)
        
        errors = []
        successes = []
        manifest_dir = "manifests/workload/mesh/auto-failover"
        
        for ctx in contexts:
            context_name = ctx['name']
            try:
                print(f"Applying Automatic Failover policies in context: {context_name}")
                process = await asyncio.create_subprocess_exec(
                    'kubectl', 'apply', '-f', manifest_dir, '--context', context_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode == 0:
                    successes.append(f"Successfully applied to {context_name}")
                    print(f"kubectl output: {stdout.decode()}")
                else:
                    errors.append(f"Error applying to {context_name}: {stderr.decode()}")
                    print(f"kubectl error: {stderr.decode()}")
                    
            except Exception as e:
                errors.append(f"Exception applying to {context_name}: {str(e)}")
                print(f"Exception for context {context_name}: {e}")
                
        if errors:
            return {
                "status": "partial_success" if successes else "error",
                "message": "Finished with errors",
                "errors": errors,
                "successes": successes
            }
            
        return {"status": "success", "message": "Automatic Failover policies applied to all contexts", "successes": successes}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/update-traffic")
async def update_traffic(cfg: TrafficConfig):
    
    """
    Updates Istio VirtualService (Weights) and DestinationRule (Outlier/Failover).
    """
    try:
        
        # 1. Update VirtualService for Traffic Splitting
        vs_api = dyn_client.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="VirtualService")
        
        # Retrieve the kubernetes virtual service deployed in the cluster in yaml format
        current_vs = vs_api.get(name="whereami", namespace=NAMESPACE)
        current_vs_yaml = yaml.dump(current_vs.to_dict())
        print(f"Current VirtualService YAML:\n{current_vs_yaml}")
        
        # Patch the existing VirtualService named 'whereami'
        vs_patch = {
            "spec": {
                "http": [{
                    "route": [
                        {
                            "destination": {"host": "whereami", "subset": "delhi"},
                            "weight": 100 - cfg.weight_mumbai
                        },
                        {
                            "destination": {"host": "whereami", "subset": "mumbai"},
                            "weight": cfg.weight_mumbai
                        }
                    ]
                }]
            }
        }
        vs_api.patch(name="whereami", namespace=NAMESPACE, body=vs_patch, content_type="application/merge-patch+json")

        # 2. Update DestinationRule for Outlier Detection & Failover
        dr_api = dyn_client.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
        
        # Define Outlier Detection settings
        outlier_settings = None
        if cfg.outlier_detection_enabled:
            outlier_settings = {
                "consecutive5xxErrors": 3,
                "interval": "10s",
                "baseEjectionTime": "30s",
                "maxEjectionPercent": 100
            }

        dr_patch = {
            "spec": {
                "trafficPolicy": {
                    "outlierDetection": outlier_settings
                }
            }
        }
        
        # If failover is specifically managed via DestinationRule locality settings
        if cfg.failover_enabled:
            dr_patch["spec"]["trafficPolicy"]["loadBalancer"] = {
                "localityLbSetting": {
                    "enabled": True,
                    "failover": [{"from": "asia-south2", "to": "asia-south1"}]
                }
            }
        else:
            dr_patch["spec"]["trafficPolicy"]["loadBalancer"] = {"simple": "ROUND_ROBIN"}

        dr_api.patch(name="whereami", namespace=NAMESPACE, body=dr_patch, content_type="application/merge-patch+json")

        return {"status": "success", "message": "Istio policies updated"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)