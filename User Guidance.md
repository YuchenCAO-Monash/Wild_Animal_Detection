Frontend Test
Step 1:
Download and unzip the Frontend file.
Step 2: 
Open terminal
$ cd <your_unzip_file>
$ npm install
$ npm run dev -- --host 127.0.0.1 --port 3000
Step 3:
Open your browser, you can register a new account and sign in to test all functions. Because the functions are all deployed in AWS Serverless Service, you do not need to deploy any other things. If you want to experience the whole process of deployment, please follow the steps as below.

GCP Cloud Run Deployment
Before starting the deployment, you need to have a GCP account for using.
Enable your Artifact Registry function in GCP.
Your device must install the “Docker Desktop” application.
Visual Studio Code is an option choice (best to use it).
Step 1:
Download and unzip the GCP
Step 2: 
Open your terminal or VScode (I will be using VScode for the demonstration)
$ gcloud auth login // login your GCP account (if there is an error occurred, install GCP CLI by using terminal)
$ gcloud config set project YOUR_PROJECT_ID // select your project
$ gcloud services enable artifactregistry.googleapis.com // enable functions
$ gcloud services enable run.googleapis.com // enable functions
$ gcloud services enable cloudbuild.googleapis.com // enable functions
$ gcloud artifacts repositories create WAD-repo \
 --repository-format=docker \
 --location=australia-southeast1 \
 --description="Docker images" // create repository
$ gcloud auth configure-docker australia-southeast2-docker.pkg.dev // configure Docker login to GCP
$ docker build -t australia-southeast2-docker.pkg.dev/YOUR_PROJECT_ID/WAD-repo/PROJECT_NAME:version .  // build docker image
$ docker push australia-southeast2-docker.pkg.dev/YOUR_PROJECT_ID/WAD-repo/PROJECT_NAME:version
$ gcloud run deploy WAD-app \
 --image australia-southeast2-docker.pkg.dev/YOUR_PROJECT_ID/WAD-repo/PROJECT_NAME:version \
 --platform managed \
 --region australia-southeast1 \
 --allow-unauthenticated // deploy the docker image on the GCP Cloud Run
