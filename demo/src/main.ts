/**
 * Main application entry point for the IntelliCache Demonstration Chatbot.
 */

import { GatewayApiClient } from './api-client'
import { DemoUIController } from './ui-controller'
import './styles.css'

document.addEventListener('DOMContentLoaded', () => {
  const apiClient = new GatewayApiClient('http://127.0.0.1:8000')
  const uiController = new DemoUIController(apiClient)
  uiController.init()
})
