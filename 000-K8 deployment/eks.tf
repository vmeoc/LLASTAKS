# === Default VPC & Subnets ===
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Details for each subnet id (map -> objects)
data "aws_subnet" "by_id" {
  for_each = toset(data.aws_subnets.default.ids)
  id       = each.value
}

# Compute subnets for the cluster (multi-AZ) and for the nodegroup (single AZ us-east-1d)
locals {
  cluster_name = "llasta"

  # All AZs available in default VPC
  azs = distinct([for s in values(data.aws_subnet.by_id) : s.availability_zone])

  # Prefer to place the GPU node group in us-east-1d (EBS volume resides there)
  ebs_az = contains(local.azs, "us-east-1d") ? "us-east-1d" : (length(local.azs) > 0 ? local.azs[0] : null)

  # Pick another AZ for the control plane requirement (EKS needs ≥ 2 AZs)
  other_azs     = [for az in local.azs : az if az != local.ebs_az]
  secondary_az  = length(local.other_azs) > 0 ? local.other_azs[0] : null
  selected_azs  = compact([local.ebs_az, local.secondary_az])

  # Cluster subnets span at least two AZs (includes us-east-1d when present)
  cluster_subnet_ids = [
    for s in values(data.aws_subnet.by_id) : s.id if contains(local.selected_azs, s.availability_zone)
  ]

  # Node group subnets restricted to the EBS AZ to allow volume attachment
  nodegroup_subnet_ids = [
    for s in values(data.aws_subnet.by_id) : s.id if s.availability_zone == local.ebs_az
  ]
}

# === REQUIRED TAGS on subnets for EKS Nodegroup ===
# Tag each selected subnet so the node group can use them.
resource "aws_ec2_tag" "subnet_cluster_shared" {
  for_each    = toset(local.cluster_subnet_ids)
  resource_id = each.value
  key         = "kubernetes.io/cluster/${local.cluster_name}"
  value       = "shared"
}

# (Optional but good to have on public subnets; harmless if already present)
resource "aws_ec2_tag" "subnet_elb" {
  for_each    = toset(local.cluster_subnet_ids)
  resource_id = each.value
  key         = "kubernetes.io/role/elb"
  value       = "1"
}

# === IAM for EKS Cluster ===
resource "aws_iam_role" "eks_cluster_role" {
  name = "eks-cluster-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Service = "eks.amazonaws.com" },
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_cluster_AmazonEKSClusterPolicy" {
  role       = aws_iam_role.eks_cluster_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# (Recommended)
resource "aws_iam_role_policy_attachment" "eks_cluster_AmazonEKSServicePolicy" {
  role       = aws_iam_role.eks_cluster_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSServicePolicy"
}

# === EKS Cluster ===
resource "aws_eks_cluster" "this" {
  name     = local.cluster_name
  role_arn = aws_iam_role.eks_cluster_role.arn
  version  = "1.33"

  vpc_config {
    subnet_ids = local.cluster_subnet_ids
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster_AmazonEKSClusterPolicy,
    aws_iam_role_policy_attachment.eks_cluster_AmazonEKSServicePolicy
  ]
}

# === OIDC Provider (requis pour EKS 1.33+ addons) ===
data "tls_certificate" "eks_oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks_oidc" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks_oidc.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

# === Rôle IAM pour EBS CSI Driver ===
resource "aws_iam_role" "ebs_csi_driver_role" {
  name = "AmazonEKS_EBS_CSI_DriverRole"
  
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRoleWithWebIdentity"
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.eks_oidc.arn
        }
        Condition = {
          StringEquals = {
            "${replace(aws_iam_openid_connect_provider.eks_oidc.url, "https://", "")}:sub" = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
            "${replace(aws_iam_openid_connect_provider.eks_oidc.url, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi_driver_policy" {
  role       = aws_iam_role.ebs_csi_driver_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# === EKS Addon: aws-ebs-csi-driver (managed via Terraform) ===
resource "aws_eks_addon" "ebs_csi" {
  cluster_name             = aws_eks_cluster.this.name
  addon_name               = "aws-ebs-csi-driver"
  service_account_role_arn = aws_iam_role.ebs_csi_driver_role.arn
  addon_version = "v1.47.0-eksbuild.1"

  # Optionally pin a version
  # addon_version = "v1.x.x-eksbuild.x"

  depends_on = [
    aws_iam_openid_connect_provider.eks_oidc,
    aws_iam_role.ebs_csi_driver_role,
    aws_iam_role_policy_attachment.ebs_csi_driver_policy,
    # Ensure nodes exist before installing addon to avoid long creation waits
    aws_eks_node_group.default,
  ]
}

# === IAM for EKS Nodes ===
resource "aws_iam_role" "eks_node_role" {
  name = "eks-node-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Service = "ec2.amazonaws.com" },
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_node_AmazonEKSWorkerNodePolicy" {
  role       = aws_iam_role.eks_node_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "eks_node_AmazonEC2ContainerRegistryReadOnly" {
  role       = aws_iam_role.eks_node_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "eks_node_AmazonEKS_CNI_Policy" {
  role       = aws_iam_role.eks_node_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

# (Optional) Add if you want SSM access to nodes later
# resource "aws_iam_role_policy_attachment" "eks_node_AmazonSSMManagedInstanceCore" {
#   role       = aws_iam_role.eks_node_role.name
#   policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
# }

# === EKS Node Group ===
resource "aws_eks_node_group" "default" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "default"
  node_role_arn   = aws_iam_role.eks_node_role.arn
  subnet_ids      = local.nodegroup_subnet_ids

  scaling_config {
    desired_size = 1
    max_size     = 1
    min_size     = 1
  }

  # GPU Spot (4 vCPU / 1 GPU)
  capacity_type  = "ON_DEMAND"
  ami_type       = "AL2023_x86_64_NVIDIA"

  # Ajout de g6.xlarge et g6e.xlarge
  instance_types = [
    "g5.xlarge",
  ]

  # Configuration du volume EBS racine
  disk_size = 100

  # Pour forcer le refresh du nodegroup si nécessaire
  force_update_version = true

  # Ensure subnets are tagged before creating the node group
  depends_on = [
    aws_ec2_tag.subnet_cluster_shared,
    aws_ec2_tag.subnet_elb,
    aws_iam_role_policy_attachment.eks_node_AmazonEKSWorkerNodePolicy,
    aws_iam_role_policy_attachment.eks_node_AmazonEC2ContainerRegistryReadOnly,
    aws_iam_role_policy_attachment.eks_node_AmazonEKS_CNI_Policy
  ]
}
