## Deployment end to end of an LLM app in #aws

<br>

## Goal

confirm my understanding of LLMs deployment and get into the details of an automated stack supporting an app. 

Be relevant in the LLM and cloud market.

Refresh my K8 and other technologies memory

Have fun

Create blog posts based on this

<br>

## Project

I want to deploy a LLM that will run in AKS, communicate with this LLM and build around it a RAG and an app (probably a chatbot). I want to monitor this LLM. I want to loRA fine tune this LLM. I want this app to be able to do function calling, for example to crawl Internet and also to code. 

I need this project to be as cheap as possible and to finish it before end of August.

Project name: LLASTA (LLM App Stack)

<br>

## File structure
Project.md: contains this project description
Diary.md: contains the diary of the project
Initial setup: contain all the yaml and instructions (stage.readme) for the initial deployment
Recurring deployment: contain all the yaml and instructions (stage.readme) for the recurring deployment

Need:

- [x] Free AWS account
- [ ] Github account
- [ ] Cluster with GPU able to host LLM
- [x] Choose the LLM. need to be accurate, good at instruction following and function calling, fine tunable. testé avec Ollama: [qwen3](https://ollama.com/library/qwen3 "qwen3")**:8b (INT4)**
- [x] Trouver container docker: [Docker vLLM openai](https://hub.docker.com/r/vllm/vllm-openai "https://hub.docker.com/r/vllm/vllm-openai") (16GB)
- [x] [Whimsical diagram](https://whimsical.com/BCS9f3idP7VFGYW5n8XbTE "https://whimsical.com/BCS9f3idP7VFGYW5n8XbTE")
- [ ] Fine tune dataset
- [ ] App code
- [ ] Orchestrator code
- [ ] Automation for all this? Notebook? Local python?

<br>

Timeline:

The BASICS:

- [ ] Create github repo
- [x] create AWS account 
- [ ] Ask for 12, 8 or 4 vCPU service quota for G instances
- [ ] deploy a cluster, ideally with 1 GPU powered node
- [ ] Host the weights & biases in S3
- [ ] Load the LLM container
- [ ] Connect to LLM
- [ ] Turn all these step as IAC or command line and make it easy to deploy it and clean it up.

<br>

The chatbot app:

- [ ] Host the chatbot code in serverless or K8
- [ ] Secure the outside connection to the chatbot code
- [ ] Automatically deploy it

<br>

Monitor

- [ ] Use Prometheus/Grafana or Cloudwatch?
- [ ] Monitor K8
- [ ] Monitor the LLM
- [ ] Monitor the app

<br>

RAG Part

- [ ] Use fake financial statement as data for the RAG. Is there a Hugging face dataset?
- [ ] deploy a vector DB
- [ ] Embed the data
- [ ] create the RAG code
- [ ] Monitor the RAG
- [ ] Make everything highly automated

<br>

Fine Tune:

- [ ] Find the dataset. Use an existing dataset and add jokes to every answer?
- [ ] Fine tune the LLM with LoRA
- [ ] Save the adapter in S3
- [ ] Load the adapter
- [ ] Test the app
- [ ] Create a switch to switch on/off the LoRA adapter

<br>

Function calling

- [ ] Update the app (the orchestrator component?) to be able to do function calling
- [ ] Find a MCP for web crawling

<br>

Code execution

- [ ] Find how to do code execution

<br>

<br>

* * *

## TODO
[ ]automatiser l'output du terraform de créationd du K8 cluster avec GPU pour le mettre dans .kube/Config
