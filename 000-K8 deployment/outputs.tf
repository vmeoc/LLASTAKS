output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "cluster_ca_certificate" {
  value = aws_eks_cluster.this.certificate_authority[0].data
}

output "node_group_gpu_name" {
  value = aws_eks_node_group.gpu.node_group_name
}

/*
output "node_group_cpu_name" {
  value = aws_eks_node_group.cpu.node_group_name
}
*/
