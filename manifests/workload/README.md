k apply -f manifests/workload/mesh/multi-region/ --context delhi && k apply -f manifests/workload/mesh/multi-region/ --context mumbai

k apply -f manifests/workload/mesh/canary/ --context delhi && k apply -f manifests/workload/mesh/canary/ --context mumbai

k apply -f manifests/workload/mesh/featureflag/ --context delhi && k apply -f manifests/workload/mesh/featureflag/ --context mumbai