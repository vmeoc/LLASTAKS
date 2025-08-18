# TODO: Option B — Manage EBS CSI addon via Terraform

Goal:
- Move aws-ebs-csi-driver addon management into Terraform for full idempotency and a single source of truth.
- Remove addon creation/update/wait logic from `Deploy.sh`.

## Files to update
- `000-K8 deployment/eks.tf`
- `Deploy.sh` (Phase 4 cleanup)

## Steps

1) In `000-K8 deployment/eks.tf`, add a Terraform resource `aws_eks_addon`:

```hcl
resource "aws_eks_addon" "ebs_csi" {
  cluster_name             = aws_eks_cluster.this.name
  addon_name               = "aws-ebs-csi-driver"
  service_account_role_arn = aws_iam_role.ebs_csi_driver_role.arn
  resolve_conflicts        = "OVERWRITE"  # alternatives: NONE, PRESERVE
  # Optional: pin version
  # addon_version = "v1.x.x-eksbuild.x"

  depends_on = [
    aws_iam_openid_connect_provider.eks_oidc,
    aws_iam_role.ebs_csi_driver_role,
    aws_iam_role_policy_attachment.ebs_csi_driver_policy,
  ]
}
```

2) In `Deploy.sh` remove the CLI-based addon management in Phase 4:
- `aws eks create-addon ...`
- `aws eks update-addon ...`
- `aws eks wait addon-active ...`

Replace the verification with an optional check:

```bash
kubectl get pods -n kube-system | grep ebs-csi || true
```

3) Keep IAM resources for IRSA in Terraform (already present):
- `aws_iam_role.ebs_csi_driver_role` (trust policy: OIDC + SA `kube-system/ebs-csi-controller-sa`)
- `aws_iam_role_policy_attachment.ebs_csi_driver_policy`

4) Apply with Terraform:

```bash
# from: 000-K8 deployment/
terraform init
terraform plan
terraform apply
```

5) Validation after apply:

```bash
kubectl get pods -n kube-system | grep ebs-csi
kubectl describe daemonset ebs-csi-node -n kube-system # optional
```

## Notes
- If the addon already exists (was created earlier via CLI), import it before apply:

```bash
# from: 000-K8 deployment/
terraform import aws_eks_addon.ebs_csi llasta:aws-ebs-csi-driver
```

- Managing the addon in Terraform eliminates CLI race conditions and "already exists" errors.
