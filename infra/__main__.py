"""The Pulumi program: one VM for the farmer, the worker and the console.

The `provider` config value picks the module that owns the cloud resources.
Each module has a deploy(config) that returns the stack's outputs, and calls
userdata.render() for the VM's first-boot script. What runs on the VM is in
infra/host/ and knows nothing about the provider.
"""

import pulumi

config = pulumi.Config()
provider = config.get("provider") or "aws"

if provider == "aws":
    import aws
    outputs = aws.deploy(config)
else:
    raise ValueError(f"provider is {provider!r}, and the only provider module is aws")

for name, value in outputs.items():
    pulumi.export(name, value)
