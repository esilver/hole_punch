package main

import (
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
)

const (
	ipEchoServiceURL = "https://api.ipify.org"
)

func getPublicIPHandler(w http.ResponseWriter, r *http.Request) {
	log.Printf("Cloud Run service 'go-ip-worker-service' (project: %s) making request to: %s", os.Getenv("GOOGLE_CLOUD_PROJECT"), ipEchoServiceURL)

	resp, err := http.Get(ipEchoServiceURL)
	if err != nil {
		errorMessage := fmt.Sprintf("Error connecting to IP echo service: %v", err)
		log.Println(errorMessage)
		http.Error(w, errorMessage, http.StatusInternalServerError)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		errorMessage := fmt.Sprintf("IP echo service returned non-OK status: %s", resp.Status)
		log.Println(errorMessage)
		http.Error(w, errorMessage, http.StatusInternalServerError)
		return
	}

	ipBody, err := io.ReadAll(resp.Body)
	if err != nil {
		errorMessage := fmt.Sprintf("Error reading response body from IP echo service: %v", err)
		log.Println(errorMessage)
		http.Error(w, errorMessage, http.StatusInternalServerError)
		return
	}

	publicIP := string(ipBody)
	logMessage := fmt.Sprintf("The IP echo service reported this service's public IP as: %s", publicIP)
	log.Println(logMessage)

	verificationNote := `VERIFICATION STEP: In the Google Cloud Console for project '%s', navigate to 'VPC Network' -> 'Cloud NAT'. 
Select the 'ip-worker-nat' (or your NAT gateway name) gateway in the '%s' region. 
Confirm that the IP address reported above is listed under 'NAT IP addresses' for that gateway.`
	verificationNoteFormatted := fmt.Sprintf(verificationNote, os.Getenv("GOOGLE_CLOUD_PROJECT"), os.Getenv("GOOGLE_CLOUD_REGION"))

	htmlResponse := fmt.Sprintf("<html><body><h1>Observed Public IP</h1><p>%s</p><hr><h2>Verification</h2><p>%s</p></body></html>",
		logMessage,
		verificationNoteFormatted)
	
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprint(w, htmlResponse)
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
		log.Printf("Defaulting to port %s", port)
	}

	http.HandleFunc("/", getPublicIPHandler)

	log.Printf("Starting Go IP checker service on port %s", port)
	if err := http.ListenAndServe(":"+port, nil); err != nil {
		log.Fatalf("Failed to start server: %v", err)
	}
} 